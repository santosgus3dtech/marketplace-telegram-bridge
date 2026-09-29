"""Safely prepare, activate, or disable the temporary Gmail monitor."""

import argparse
import getpass
import os
import re
import stat
import tempfile
from datetime import datetime
from pathlib import Path

DEFAULT_ENV_FILE = Path(".env")
DEFAULT_SENDER_DOMAINS = "olx.com.br,olxbr.com"
DEFAULT_SUBJECT = "Atendimento OLX"
_ENV_KEY = re.compile(r"^[A-Z][A-Z0-9_]*$")


def validate_cutoff(value: str) -> str:
    """Return an ISO cutoff only when it contains an explicit UTC offset."""

    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError("the cutoff must include a UTC offset")
    return parsed.isoformat()


def read_env(path: Path) -> dict[str, str]:
    """Read simple dotenv assignments without expanding or logging values."""

    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        if _ENV_KEY.fullmatch(key):
            values[key] = value
    return values


def update_env(path: Path, updates: dict[str, str]) -> None:
    """Atomically upsert selected dotenv values while preserving all other lines."""

    if not path.exists() or not path.is_file() or path.is_symlink():
        raise RuntimeError(f"expected an existing regular environment file: {path}")
    invalid = [key for key in updates if _ENV_KEY.fullmatch(key) is None]
    if invalid:
        raise ValueError("invalid environment key")

    original_mode = stat.S_IMODE(path.stat().st_mode)
    pending = dict(updates)
    output: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key = line.split("=", 1)[0].strip()
            if key in pending:
                output.append(f"{key}={pending.pop(key)}")
                continue
        output.append(line)
    if output and output[-1] != "":
        output.append("")
    output.extend(f"{key}={value}" for key, value in pending.items())

    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=path.parent,
            prefix=f".{path.name}.",
            delete=False,
        ) as temporary:
            temporary.write("\n".join(output) + "\n")
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary_path = Path(temporary.name)
        os.chmod(temporary_path, original_mode)
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()


def prepare(path: Path, *, username: str, not_before: str) -> None:
    """Write non-secret filters and keep the monitor disabled."""

    if not username.strip() or "@" not in username:
        raise ValueError("a valid Gmail username is required")
    update_env(
        path,
        {
            "GMAIL_REPLY_WATCH_ENABLED": "false",
            "GMAIL_IMAP_USERNAME": username.strip(),
            "GMAIL_IMAP_APP_PASSWORD": "",
            "GMAIL_REPLY_WATCH_SENDER_DOMAIN": DEFAULT_SENDER_DOMAINS,
            "GMAIL_REPLY_WATCH_SUBJECT": DEFAULT_SUBJECT,
            "GMAIL_REPLY_WATCH_NOT_BEFORE": not_before,
            "GMAIL_REPLY_WATCH_STOP_AFTER_MATCH": "true",
        },
    )


def activate(path: Path) -> None:
    """Prompt privately for an app password and enable the prepared monitor."""

    values = read_env(path)
    required = (
        "GMAIL_IMAP_USERNAME",
        "GMAIL_REPLY_WATCH_SENDER_DOMAIN",
        "GMAIL_REPLY_WATCH_SUBJECT",
        "GMAIL_REPLY_WATCH_NOT_BEFORE",
    )
    if any(not values.get(key, "").strip() for key in required):
        raise RuntimeError("prepare the Gmail monitor before activating it")
    password = getpass.getpass("Gmail app password (input hidden): ").replace(" ", "")
    if re.fullmatch(r"[A-Za-z0-9]{16}", password) is None:
        raise ValueError("the Gmail app password must contain 16 letters or digits")
    update_env(
        path,
        {
            "GMAIL_IMAP_APP_PASSWORD": password,
            "GMAIL_REPLY_WATCH_ENABLED": "true",
        },
    )


def disable(path: Path) -> None:
    """Stop the monitor and remove its Gmail credential from the environment file."""

    update_env(
        path,
        {
            "GMAIL_REPLY_WATCH_ENABLED": "false",
            "GMAIL_IMAP_APP_PASSWORD": "",
        },
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    actions = parser.add_mutually_exclusive_group(required=True)
    actions.add_argument("--prepare", action="store_true")
    actions.add_argument("--activate", action="store_true")
    actions.add_argument("--disable", action="store_true")
    parser.add_argument("--username")
    parser.add_argument("--not-before", type=validate_cutoff)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        if args.prepare:
            if args.username is None or args.not_before is None:
                raise ValueError("--prepare requires --username and --not-before")
            prepare(args.env_file, username=args.username, not_before=args.not_before)
            print("Gmail monitor prepared and disabled; no app password is stored.")
        elif args.activate:
            activate(args.env_file)
            print("Gmail monitor enabled; the app password was stored without being displayed.")
        else:
            disable(args.env_file)
            print("Gmail monitor disabled and its app password removed.")
    except (OSError, RuntimeError, ValueError) as error:
        print(f"Configuration failed: {error}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
