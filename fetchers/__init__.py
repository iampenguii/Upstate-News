"""Feed fetchers for the Upstate SC feed bot.

Each module here knows how to pull one kind of source and normalize it into
plain dicts. The fetchers do no Discord posting and no state management; that
is main.py's job.
"""
