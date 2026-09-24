def format_minor_amount(amount_minor: int, currency: str) -> str:
    """Format a feed amount for model-facing catalog text."""
    units, cents = divmod(amount_minor, 100)
    rendered = f"{units:,}".replace(",", ".")
    if cents:
        rendered = f"{rendered},{cents:02d}"
    return f"{currency.upper()} {rendered}"
