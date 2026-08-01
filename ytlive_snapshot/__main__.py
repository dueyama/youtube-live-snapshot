"""Allow ``python -m ytlive_snapshot`` execution."""

from .cli import main


if __name__ == "__main__":
    raise SystemExit(main())
