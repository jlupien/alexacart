"""
Patches applied to nodriver for compatibility with newer Chrome versions.

1. Browser connection timeout — Chrome on Apple Silicon (Rosetta) can take
   5+ seconds to start. nodriver's default timeout is ~2.75s. Increased to ~10s.

2. CDP Cookie.from_json — Chrome 146 dropped the `sameParty` field but nodriver
   0.48.1 treats it as required, causing constant KeyError spam in the logs.

3. CDP ClientSecurityState.from_json — Chrome 146 dropped `privateNetworkRequestPolicy`
   from some events; same issue.

Import this module and call patch() before any nodriver usage.
"""

import asyncio
import logging
import pathlib
import warnings

import nodriver.cdp.network as _network_mod
import nodriver.core.browser as _browser_mod
from nodriver.core.browser import (
    Connection,
    ContraDict,
    HTTPApi,
    cdp,
    is_posix,
    util,
)

logger = logging.getLogger(__name__)

_PATCHED = False

# Increased from originals: retries=5 sleep=0.5 initial=0.25
_RETRIES = 20
_SLEEP = 0.5
_INITIAL_WAIT = 0.5


def patch():
    """Replace Browser.start with a version that has a longer connection timeout."""
    global _PATCHED
    if _PATCHED:
        return
    _PATCHED = True

    async def _start_with_longer_timeout(self=None):
        """Copy of Browser.start() with increased connection retry count."""
        if not self:
            warnings.warn("use ``await Browser.create()`` to create a new instance")
            return

        if self._process or self._process_pid:
            if self._process.returncode is not None:
                return await self.create(config=self.config)
            warnings.warn("ignored! this call has no effect when already running.")
            return

        connect_existing = False
        if self.config.host is not None and self.config.port is not None:
            connect_existing = True
        else:
            self.config.host = "127.0.0.1"
            self.config.port = util.free_port()

        if not connect_existing:
            if not pathlib.Path(self.config.browser_executable_path).exists():
                raise FileNotFoundError(
                    "Could not determine browser executable. "
                    "Make sure your browser is installed in the default location."
                )

        if getattr(self.config, "_extensions", None):
            self.config.add_argument(
                "--load-extension=%s"
                % ",".join(str(_) for _ in self.config._extensions)
            )

        exe = self.config.browser_executable_path
        params = self.config()

        logger.info(
            "starting\n\texecutable :%s\n\narguments:\n%s",
            exe,
            "\n\t".join(params),
        )
        if not connect_existing:
            self._process = await asyncio.create_subprocess_exec(
                exe,
                *params,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                close_fds=is_posix,
            )
            self._process_pid = self._process.pid

        self._http = HTTPApi((self.config.host, self.config.port))
        util.get_registered_instances().add(self)

        # --- PATCHED: longer timeout for slow Chrome startup (e.g. Rosetta) ---
        await asyncio.sleep(_INITIAL_WAIT)
        for i in range(_RETRIES):
            try:
                self.info = ContraDict(
                    await self._http.get("version"), silent=True
                )
            except (Exception,):
                if i == _RETRIES - 1:
                    logger.debug("could not start", exc_info=True)
                await asyncio.sleep(_SLEEP)
            else:
                break

        if not self.info:
            raise Exception(
                """
                ---------------------
                Failed to connect to browser
                ---------------------
                One of the causes could be when you are running as root.
                In that case you need to pass no_sandbox=True
                """
            )

        self.connection = Connection(
            self.info.webSocketDebuggerUrl, browser=self
        )

        if self.config.autodiscover_targets:
            logger.info("enabling autodiscover targets")
            self.connection.handlers[cdp.target.TargetInfoChanged] = [
                self._handle_target_update
            ]
            self.connection.handlers[cdp.target.TargetCreated] = [
                self._handle_target_update
            ]
            self.connection.handlers[cdp.target.TargetDestroyed] = [
                self._handle_target_update
            ]
            self.connection.handlers[cdp.target.TargetCrashed] = [
                self._handle_target_update
            ]
            await self.connection.send(
                cdp.target.set_discover_targets(discover=True)
            )

        await self.update_targets()
        await self

    _browser_mod.Browser.start = _start_with_longer_timeout
    logger.debug("Patched nodriver browser connection timeout (%d retries)", _RETRIES)

    # --- Patch 2: Cookie.from_json — handle missing 'sameParty' (dropped in Chrome 146) ---
    _orig_cookie_from_json = _network_mod.Cookie.from_json.__func__

    @classmethod  # type: ignore[misc]
    def _cookie_from_json(cls, json):
        if 'sameParty' not in json:
            json = {**json, 'sameParty': False}
        return _orig_cookie_from_json(cls, json)

    _network_mod.Cookie.from_json = _cookie_from_json
    logger.debug("Patched nodriver Cookie.from_json (sameParty optional)")

    # --- Patch 3: ClientSecurityState.from_json — handle missing 'privateNetworkRequestPolicy' ---
    _orig_css_from_json = _network_mod.ClientSecurityState.from_json.__func__

    @classmethod  # type: ignore[misc]
    def _client_security_state_from_json(cls, json):
        if 'privateNetworkRequestPolicy' not in json:
            json = {**json, 'privateNetworkRequestPolicy': 'Allow'}
        return _orig_css_from_json(cls, json)

    _network_mod.ClientSecurityState.from_json = _client_security_state_from_json
    logger.debug("Patched nodriver ClientSecurityState.from_json (privateNetworkRequestPolicy optional)")
