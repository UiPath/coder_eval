"""Standalone proxy command — run the LLM Gateway proxy for use with Claude Code CLI."""

import asyncio
import contextlib
import signal
from collections.abc import Callable

import typer


def _install_proxy_signal_handlers(loop: asyncio.AbstractEventLoop, handler: Callable[[], None]) -> None:
    """Wire SIGINT/SIGTERM to ``handler`` with a Windows fallback.

    The asyncio Proactor event loop on Windows does not implement
    ``loop.add_signal_handler``. We try the loop-based registration first
    (the POSIX happy path) and fall back to the synchronous ``signal.signal``
    API if NotImplementedError is raised — that path covers Windows, and is
    safe here because the handler is trivial (sets an Event).

    The fallback covers both signals after a single failure; if SIGINT
    succeeds and SIGTERM later raises, re-registering SIGINT through
    ``signal.signal`` is last-write-wins harmless.
    """
    try:
        loop.add_signal_handler(signal.SIGINT, handler)
        loop.add_signal_handler(signal.SIGTERM, handler)
    except NotImplementedError:
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, lambda *_args: handler())


def proxy_command(
    port: int = typer.Option(0, help="Port to bind to (0 = auto-assign)"),
    env_file: str = typer.Option(".env", help="Path to .env file with LLM Gateway credentials"),
    vendor: str = typer.Option("awsbedrock", help="Gateway vendor (awsbedrock, anthropic)"),
    api_flavor: str = typer.Option("invoke", help="Gateway API flavor"),
    quiet: bool = typer.Option(False, "--quiet", "-q", help="Only print export commands (for eval-style usage)"),
) -> None:
    """Start a local proxy that routes Anthropic API calls through the UiPath LLM Gateway.

    This lets you use Claude Code CLI without ANTHROPIC_API_KEY or CLAUDE_CODE_OAUTH_TOKEN.
    The proxy handles OAuth2 S2S authentication with the gateway transparently.

    Usage with Claude Code:

        # Terminal 1: start proxy
        coder-eval proxy --port 8080

        # Terminal 2: use claude
        export ANTHROPIC_BASE_URL=http://127.0.0.1:8080
        export ANTHROPIC_API_KEY=llmgw-proxy
        claude

    Or in a script / CI:

        eval "$(coder-eval proxy --port 8080 -q &)"
        sleep 2
        claude -p "hello"

    Required environment variables (or in .env file):

        LLMGW_URL, LLMGW_CLIENT_ID, LLMGW_CLIENT_SECRET,
        LLMGW_SEMANTIC_ORG_ID, LLMGW_SEMANTIC_TENANT_ID
    """
    with contextlib.suppress(KeyboardInterrupt, SystemExit):
        asyncio.run(_run_proxy(port, env_file, vendor, api_flavor, quiet))


async def _run_proxy(port: int, env_file: str, vendor: str, api_flavor: str, quiet: bool) -> None:
    """Start the proxy and block until interrupted."""
    import os

    from dotenv import load_dotenv
    from rich.console import Console

    # Use stderr for human-readable messages so stdout stays clean for eval/scripting
    err_console = Console(stderr=True)

    # Load .env file (override=True so .env values take precedence over shell env)
    load_dotenv(env_file, override=True)

    # Validate required settings
    required = {
        "LLMGW_URL": os.getenv("LLMGW_URL", ""),
        "LLMGW_CLIENT_ID": os.getenv("LLMGW_CLIENT_ID", ""),
        "LLMGW_CLIENT_SECRET": os.getenv("LLMGW_CLIENT_SECRET", ""),
        "LLMGW_SEMANTIC_ORG_ID": os.getenv("LLMGW_SEMANTIC_ORG_ID", ""),
        "LLMGW_SEMANTIC_TENANT_ID": os.getenv("LLMGW_SEMANTIC_TENANT_ID", ""),
    }
    missing = [k for k, v in required.items() if not v]

    if missing:
        err_console.print(f"[red]Missing required settings: {', '.join(missing)}[/red]")
        err_console.print("Set them in your .env file or as environment variables.")
        raise typer.Exit(1)

    timeout_raw = os.getenv("LLMGW_TIMEOUT_SECONDS", "300")
    try:
        timeout_seconds = int(timeout_raw)
    except ValueError as err:
        err_console.print(f"[red]Invalid LLMGW_TIMEOUT_SECONDS: {timeout_raw!r} (must be an integer)[/red]")
        raise typer.Exit(1) from err

    from coder_eval.proxy.config import ProxyConfig
    from coder_eval.proxy.server import LLMGatewayProxy

    config = ProxyConfig(
        llmgw_url=required["LLMGW_URL"],
        client_id=required["LLMGW_CLIENT_ID"],
        client_secret=required["LLMGW_CLIENT_SECRET"],
        org_id=required["LLMGW_SEMANTIC_ORG_ID"],
        tenant_id=required["LLMGW_SEMANTIC_TENANT_ID"],
        requesting_product=os.getenv("LLMGW_REQUESTING_PRODUCT", "coder-eval"),
        requesting_feature=os.getenv("LLMGW_REQUESTING_FEATURE", "claude-code-agent"),
        user_id=os.getenv("LLMGW_SEMANTIC_USER_ID", ""),
        timeout_seconds=timeout_seconds,
        vendor=vendor,
        api_flavor=api_flavor,
    )

    proxy = LLMGatewayProxy(config)
    actual_port = await proxy.start(port=port)

    # Always print export commands (for eval-style usage and for the user)
    # Use stderr for human-readable messages when quiet, stdout for exports
    if quiet:
        # In quiet mode, print only the export commands to stdout (for eval)
        print(f"export ANTHROPIC_BASE_URL=http://127.0.0.1:{actual_port}")
        print("export ANTHROPIC_API_KEY=llmgw-proxy")
    else:
        err_console.print()
        err_console.print("[bold green]LLM Gateway Proxy running[/bold green]")
        err_console.print(f"  URL:    http://127.0.0.1:{actual_port}")
        err_console.print(f"  Gateway: {required['LLMGW_URL']}")
        err_console.print(f"  Vendor:  {vendor}")
        err_console.print()
        err_console.print("[bold]To use with Claude Code, run in another terminal:[/bold]")
        err_console.print(f"  export ANTHROPIC_BASE_URL=http://127.0.0.1:{actual_port}")
        err_console.print("  export ANTHROPIC_API_KEY=llmgw-proxy")
        err_console.print("  claude")
        err_console.print()
        err_console.print("[dim]Press Ctrl+C to stop[/dim]")

    # Block until interrupted
    stop_event = asyncio.Event()

    def _signal_handler() -> None:
        stop_event.set()

    _install_proxy_signal_handlers(asyncio.get_running_loop(), _signal_handler)

    try:
        await stop_event.wait()
    finally:
        if not quiet:
            err_console.print("\n[yellow]Shutting down proxy...[/yellow]")
        await proxy.stop()
        usage = proxy.usage
        if usage.requests > 0 and not quiet:
            cost_str = f"${usage.total_cost:.4f}"
            summary = (
                f"[dim]Total: {usage.requests} requests, "
                + f"{usage.input_tokens} input + {usage.output_tokens} output tokens, "
                + f"cost: {cost_str}[/dim]"
            )
            err_console.print(summary)
        if not quiet:
            err_console.print("[green]Proxy stopped.[/green]")
