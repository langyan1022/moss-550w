import asyncio
from dataclasses import dataclass, field
import hmac
import secrets
from time import monotonic

from aiohttp import web


@dataclass
class WakeOffer:
    ticket: str = field(default_factory=secrets.token_urlsafe)
    claimed: asyncio.Event = field(default_factory=asyncio.Event)
    shown: asyncio.Event = field(default_factory=asyncio.Event)
    grant: str | None = None
    listen_next: bool = False
    expires_at: float | None = None


class BrowserWake:
    def __init__(self, token):
        self.token = token
        self.pages = {}
        self.offer = None
        self.launch_offer = None

    def connected_pages(self):
        return [signal for signal, transport in self.pages.items() if transport and not transport.is_closing()]

    def needs_browser(self, result, listen_next):
        if not listen_next:
            return {"result": result}
        self.launch_offer = WakeOffer(listen_next=True, expires_at=monotonic() + 15)
        return {"result": result, "launch_ticket": self.launch_offer.ticket}

    async def activate(self, listen_next=False):
        if self.offer is not None:
            raise web.HTTPConflict()
        self.launch_offer = None
        pages = self.connected_pages()
        if not pages:
            return self.needs_browser("no_page", listen_next)
        offer = self.offer = WakeOffer(listen_next=listen_next)
        try:
            started = monotonic()
            for signal in pages:
                signal.set()
            await asyncio.wait_for(offer.claimed.wait(), .55)
            # A foreground page that claimed the wake may need another frame under load.
            if not offer.shown.is_set():
                await asyncio.wait_for(offer.shown.wait(), max(0, 1.0 - (monotonic() - started)))
            return {"result": "page_shown"}
        except TimeoutError:
            return self.needs_browser("no_receipt", listen_next)
        finally:
            self.offer = None

    async def handle(self, request):
        try:
            body = await request.json()
        except (ValueError, UnicodeDecodeError):
            raise web.HTTPBadRequest()
        if not isinstance(body, dict):
            raise web.HTTPBadRequest()
        action = request.match_info["wake_action"]
        if action in {"activate", "status"}:
            supplied = body.get("token")
            if not isinstance(supplied, str) or not supplied.isascii() or not hmac.compare_digest(supplied, self.token):
                raise web.HTTPForbidden()
            result = ({"page_connected": bool(self.connected_pages())} if action == "status"
                      else await self.activate(body.get("listen_next") is True))
            return web.json_response(result)

        if request.headers.get("Origin") != f"{request.scheme}://{request.host}":
            raise web.HTTPForbidden()
        ticket = body.get("ticket")
        offer = next((item for item in (self.offer, self.launch_offer) if item and item.ticket == ticket), None)
        if (offer is None or offer.shown.is_set()
                or (offer.expires_at is not None and offer.expires_at <= monotonic())):
            raise web.HTTPGone()
        if action == "claim":
            if offer.grant is not None:
                raise web.HTTPConflict()
            offer.grant = secrets.token_urlsafe()
            offer.claimed.set()
            return web.json_response({"grant": offer.grant, **({"listen_next": True} if offer.listen_next else {})})
        if offer.grant is None or body.get("grant") != offer.grant:
            raise web.HTTPForbidden()
        offer.shown.set()
        if offer is self.launch_offer:
            self.launch_offer = None
        return web.Response(status=204)
