"""Routes each customer message to one of the five agents and wires in live order tracking."""

import asyncio
import logging
import time

import anthropic

from app import claude_code, config, fastpath, llm, routing
from app.agents import AGENTS
from app.agents.base import AgentContext
from app.agents.tracker import OrderTracker, eta_minutes, status_line
from app.models import Order, OrderStatus
from app.session import Session
from app.store import Store

log = logging.getLogger(__name__)

TRACKER_LABEL = AGENTS["tracker"].label


class Orchestrator:
    def __init__(self, store: Store, delays: dict[str, float] = config.STATUS_DELAYS):
        self.store = store
        self.sessions: dict[str, Session] = {}
        self.tracker = OrderTracker(store, self._on_status, delays)
        self._order_sessions: dict[str, Session] = {}
        self._background: set[asyncio.Task] = set()

    def session(self, customer_id: str) -> Session:
        if customer_id not in self.store.customers:
            raise KeyError(customer_id)
        return self.sessions.setdefault(customer_id, Session(customer_id=customer_id))

    # ---------- Services protocol (used by agent tools) ----------

    def start_tracking(self, session: Session, order: Order) -> None:
        self._order_sessions[order.id] = session
        self.tracker.start(order)

    def stop_tracking(self, order_id: str) -> None:
        self.tracker.stop(order_id)

    # ---------- entry points ----------

    async def greet(self, session: Session) -> None:
        """First connection: a personalised greeting built from the taste profile (no LLM)."""
        async with session.lock:
            if session.greeted:
                return
            session.greeted = True
            await self._say_fast(session, "recommender", fastpath.greeting(self._ctx(session)))

    async def handle(self, session: Session, text: str) -> None:
        session.pending.append(text)
        if session.draining:
            return  # the loop below is already running and will pick this message up
        session.draining = True
        try:
            async with session.lock:
                # Messages sent while an agent was busy are merged into one turn, so the
                # customer gets one up-to-date answer instead of a queue of stale replies.
                while session.pending:
                    texts, session.pending = session.pending, []
                    session.turn += 1
                    merged = "\n".join(texts)
                    session.add_message("user", merged)
                    agent = await self.pick_agent(session, merged)
                    reply = fastpath.try_handle(agent, self._ctx(session), merged)
                    if reply is not None:
                        await self._say_fast(session, agent, reply)
                    else:
                        await self._run_agent(session, agent)
        finally:
            session.draining = False

    async def pick_agent(self, session: Session, text: str = "") -> str:
        agent = routing.quick_route(session, text)
        if agent:
            log.info("route -> %s (rule)", agent)
            return agent
        try:
            route = await llm.route(self._transcript(session), self._state(session))
        except Exception:
            log.exception("router failed")
            return "followup" if session.feedback_order_id else "intake"
        log.info("route -> %s (%s)", route.agent, route.reason)
        return route.agent

    # ---------- internals ----------

    def _ctx(self, session: Session) -> AgentContext:
        return AgentContext(store=self.store, session=session, services=self)

    async def _say_fast(self, session: Session, agent: str, text: str) -> None:
        """Send a deterministic (no-LLM) reply on behalf of an agent."""
        log.info("agent %s replied via fast path (no LLM)", agent)
        session.last_agent = agent
        await session.say(AGENTS[agent].label, text)
        await session.emit({"type": "cart", "items": session.cart_view(self.store)})

    def build_prompt(self, session: Session, name: str) -> tuple[str, str]:
        """Return (system, volatile) for one agent run, laid out for prompt caching.

        `system` holds only content that is identical for every customer and every turn
        (agent rules + menu), so its cached prefix is reused across messages and customers.
        Everything that changes (customer, notes, session state, cart) goes in `volatile`,
        which the backends send after the conversation.
        """
        spec = AGENTS[name]
        system = spec.system
        if spec.static_context:
            system += "\n\n" + spec.static_context(self.store)
        customer = self.store.customers[session.customer_id]
        volatile = (f"Customer: {customer.name} (id {customer.id}, loyalty "
                    f"{customer.loyalty_tier}).\nSession state:\n{self._state(session)}")
        if customer.notes:
            volatile += f"\nIMPORTANT customer notes: {customer.notes}"
        if spec.context:
            ctx = AgentContext(store=self.store, session=session, services=self)
            volatile += "\n\n" + spec.context(ctx)
        return system, volatile

    async def _run_agent(self, session: Session, name: str, event: str | None = None) -> None:
        spec = AGENTS[name]
        await session.emit({"type": "typing", "agent": spec.label})
        ctx = AgentContext(store=self.store, session=session, services=self)
        system, volatile = self.build_prompt(session, name)
        messages = list(session.history)
        started = time.monotonic()
        if event:
            # System events (e.g. "order delivered") are shown to the agent but not stored.
            messages.append({"role": "user", "content": event})
        try:
            text = await llm.run_agent(system, spec.make_tools(ctx), messages, spec.effort,
                                       agent=name, customer_id=session.customer_id,
                                       volatile=volatile)
        except anthropic.RateLimitError:
            text = "We're a bit busy right now. Please try again in a moment."
        except anthropic.APIConnectionError:
            text = "I couldn't reach our ordering brain. Please check the connection and retry."
        except anthropic.APIStatusError as e:
            log.exception("agent %s failed", name)
            text = f"Something went wrong on our side ({e.status_code}). Please try again."
        except TypeError as e:
            if "authentication" not in str(e):
                raise
            log.error("No Anthropic credentials: set ANTHROPIC_API_KEY in .env and restart")
            text = ("I can't reach Claude: no API key is configured. Add ANTHROPIC_API_KEY to "
                    "the .env file in the project folder and restart the server.")
        except claude_code.ClaudeCodeTimeout:
            log.error("agent %s timed out", name)
            text = "Sorry, that took too long on my side. Could you send that again?"
        except claude_code.ClaudeCodeError as e:
            log.error("Claude Code backend failed: %s", e)
            text = ("I couldn't get a reply from Claude Code. Check that `claude` works in a "
                    "terminal and that you're logged in (and that the PC isn't out of memory).")
        except Exception:  # never leave the chat hanging on "typing"
            log.exception("agent %s crashed", name)
            text = "Something went wrong on our side. Please try again."
        log.info("agent %s replied in %.1fs", name, time.monotonic() - started)
        session.last_agent = name
        await session.say(spec.label, text)
        await session.emit({"type": "cart", "items": session.cart_view(self.store)})

    async def _on_status(self, order: Order) -> None:
        """Called by OrderTracker on every status change: push a live update to the chat."""
        session = self._order_sessions.get(order.id)
        if session is None:
            return
        await session.emit({"type": "status", "order_id": order.id, "status": order.status.value,
                            "eta_minutes": eta_minutes(order)})
        await session.say(TRACKER_LABEL, status_line(order))
        if order.status == OrderStatus.DELIVERED:
            if session.active_order_id == order.id:
                session.active_order_id = None
            session.feedback_order_id = order.id
            task = asyncio.create_task(self._start_followup(session, order))
            self._background.add(task)
            task.add_done_callback(self._background.discard)

    async def _start_followup(self, session: Session, order: Order) -> None:
        async with session.lock:
            await self._say_fast(session, "followup", fastpath.feedback_request(self._ctx(session)))

    def _state(self, session: Session) -> str:
        active = self.store.orders.get(session.active_order_id or "")
        return "\n".join([
            f"- cart lines: {len(session.cart)}",
            f"- price summary shown, awaiting confirmation: {session.quoted_turn is not None}",
            f"- promo applied: {session.promo_code or 'none'}",
            f"- active order: {f'{active.id} ({active.status.value})' if active else 'none'}",
            f"- delivered order awaiting feedback: {session.feedback_order_id or 'none'}",
        ])

    @staticmethod
    def _transcript(session: Session, last: int = 8) -> str:
        who = {"user": "Customer", "assistant": "Shop"}
        return "\n".join(f"{who[m['role']]}: {m['content']}" for m in session.history[-last:])
