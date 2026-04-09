"""
main.py ? trading bot entry point

Startup sequence:
  1. config/settings.py loads and validates
  2. shared.py inits + GET /clock sets MARKET_OPEN immediately
  3. alpaca/client.py connects (TradingClient)
  4. ref_library starts ? ref_ready_event fires (always, even on error)
  5. stream.py registers all callbacks ? stream_ready_event fires
  6. account_agent + diagnostics + signal_gen start ? account_ready_event fires
  7. boss + risk_manager + order_exec start. SIGTERM handler registered.
"""

import time
import signal
import logging
import threading
import sys
import shared
from config import settings
from storage import database
from alpaca_local import client as alpaca, stream as alpaca_stream
from agents import (
    boss, signal_generator, risk_manager,
    order_execution, account_agent, ref_library, diagnostics,
    plan_manager,
)

# ?? logging setup ?????????????????????????????????????????????
logging.basicConfig(
    level=settings.LOG_LEVEL,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("main")

# ?? supervisor wrapper ?????????????????????????????????????????
def _supervised(fn, name: str):
    """Wrap an agent's run() in a crash-restart loop with exponential backoff."""
    def _inner():
        restart_n = 0
        while not shared.SHUTTING_DOWN:
            try:
                fn()
            except Exception as exc:
                if shared.SHUTTING_DOWN:
                    break
                restart_n += 1
                err_msg = f"{type(exc).__name__}: {exc}"
                logger.error(f"supervisor: agent '{name}' crashed (restart #{restart_n}): {err_msg}")

                with shared.errors_lock:
                    shared.AGENT_ERRORS[name] = {
                        "count": restart_n,
                        "last_error": err_msg,
                        "last_ts": time.time(),
                    }
                database.write_agent_log(
                    ts=time.time(), agent=name, level="ERROR",
                    message=err_msg, restart_n=restart_n,
                )

                backoff = min(settings.BACKOFF_BASE ** restart_n, 300)
                logger.info(f"supervisor: restarting '{name}' in {backoff:.0f}s")
                time.sleep(backoff)
    return _inner

def _start_thread(fn, name: str, daemon=True) -> threading.Thread:
    t = threading.Thread(target=_supervised(fn, name), name=name, daemon=daemon)
    t.start()
    return t

# ?? SIGTERM / SIGINT handler ???????????????????????????????????
_all_threads: list = []

def _shutdown(signum, frame):
    logger.info(f"main: received signal {signum} ? initiating graceful shutdown")
    shared.SHUTTING_DOWN = True

signal.signal(signal.SIGTERM, _shutdown)
signal.signal(signal.SIGINT,  _shutdown)

# ?? main ???????????????????????????????????????????????????????
def main():
    logger.info("=" * 60)
    logger.info(f"trading bot starting ? IS_PAPER={settings.IS_PAPER}")
    logger.info(f"  BASE_URL        = {settings.BASE_URL}")
    logger.info(f"  DATA_FEED       = {settings.DATA_FEED}")
    logger.info(f"  OPTIONS_LEVEL   = {settings.OPTIONS_LEVEL}")
    logger.info(f"  MAX_POSITION_SIZE = ${settings.MAX_POSITION_SIZE:,.0f}")
    logger.info(f"  MAX_PORTFOLIO_PCT = {settings.MAX_PORTFOLIO_PCT:.0%}")
    logger.info("=" * 60)

    # ?? step 1: already done (settings imported above) ????????
    logger.info("step 1: config loaded and validated")

    # ?? step 2: shared.py init + GET /clock ???????????????????
    database.init_db()
    try:
        clock = alpaca.get_clock()
        shared.MARKET_OPEN = clock.is_open
        logger.info(f"step 2: MARKET_OPEN={shared.MARKET_OPEN}")
    except Exception as e:
        logger.warning(f"step 2: GET /clock failed, defaulting MARKET_OPEN=False: {e}")

    # ?? step 3: client connection confirmed ???????????????????
    try:
        acct = alpaca.get_account()
        logger.info(f"step 3: connected ? account status={acct.status}")
    except Exception as e:
        logger.error(f"step 3: cannot connect to Alpaca API: {e}")
        sys.exit(1)

    # -- step 3b: resolve initial watchlist from settings --------
    initial_wl = list(settings.WATCHLIST) + list(settings.CRYPTO_WATCHLIST)
    with shared.cache_lock:
        shared.watchlist = initial_wl
    logger.info(f"step 3b: initial watchlist -> {len(initial_wl)} symbols")

    # ?? step 4: ref library ? wait for ref_ready_event ????????
    t_ref = _start_thread(ref_library.run, "ref_library")
    _all_threads.append(t_ref)
    logger.info("step 4: ref_library started ? waiting for ref_ready_event")
    shared.ref_ready_event.wait(timeout=120)
    if shared.ref_load_error:
        logger.warning("step 4: ref_library completed with partial errors ? continuing")
    else:
        logger.info("step 4: ref_ready_event received")

    # ?? step 5: stream callbacks registered ? stream_ready_event
    # Register fill callbacks before starting streams
    # (order_exec and account_agent register their own callbacks in their run() methods,
    #  but stream.start() must be called here before those threads launch)
    logger.info("step 5: starting streams")
    alpaca_stream.start()
    shared.stream_ready_event.wait(timeout=30)
    logger.info("step 5: stream_ready_event received - stream threads launched (WebSocket connections completing async)")

    # -- step 6: independents - wait for account_ready_event ----
    t_acct = _start_thread(account_agent.run,    "account_agent")
    t_diag = _start_thread(diagnostics.run,      "diagnostics")
    t_sig  = _start_thread(signal_generator.run, "signal_generator")
    t_plan = _start_thread(plan_manager.run,     "plan_manager")
    _all_threads.extend([t_acct, t_diag, t_sig, t_plan])

    logger.info("step 6: account_agent / diagnostics / signal_generator / plan_manager started")
    logger.info("step 6: waiting for account_ready_event")
    shared.account_ready_event.wait(timeout=30)
    logger.info("step 6: account_ready_event received - positions populated")

    # ?? step 7: boss + managed agents ?????????????????????????
    t_boss = _start_thread(boss.run,            "boss")
    t_risk = _start_thread(risk_manager.run,    "risk_manager")
    t_exec = _start_thread(order_execution.run, "order_execution")
    _all_threads.extend([t_boss, t_risk, t_exec])
    logger.info("step 7: boss / risk_manager / order_execution started")
    logger.info("all agents running ? bot is live")

    # ?? main thread: wait for shutdown ????????????????????????
    try:
        while not shared.SHUTTING_DOWN:
            time.sleep(1)
    except KeyboardInterrupt:
        shared.SHUTTING_DOWN = True

    logger.info("main: waiting for all threads to exit")
    for t in _all_threads:
        t.join(timeout=settings.CANCEL_TIMEOUT + 5)

    logger.info("main: clean exit")

if __name__ == "__main__":
    main()
