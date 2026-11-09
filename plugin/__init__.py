"""Independent private-DM Telegram transports for one shared Hermes profile."""
from functools import partial
import re
from .adapter import MultiBotAdapter


def register(ctx):
    bots = ctx.get_config('bots', [])
    if not isinstance(bots, list) or len(bots) > 8:
        raise ValueError('bots must be a list of at most eight slots')
    prepared, aliases, slots = [], set(), set()
    for bot in bots:
        if not isinstance(bot, dict) or set(bot) != {'slot', 'alias', 'allowed_users'}:
            raise ValueError('bot requires only slot, alias and allowed_users')
        slot, alias, users = bot['slot'], bot['alias'], bot['allowed_users']
        if type(slot) is not int or not 1 <= slot <= 8 or slot in slots:
            raise ValueError('unique integer slot 1..8 required')
        if not isinstance(alias, str) or not re.fullmatch(r'[a-z][a-z0-9_]{0,23}', alias) or alias in aliases:
            raise ValueError('unique alias matching [a-z][a-z0-9_]{0,23} required')
        if not isinstance(users, list) or not users or any(
            not isinstance(u, str) or re.fullmatch(r'[1-9][0-9]*', u) is None for u in users
        ):
            raise ValueError('nonempty numeric string allowed_users required')
        aliases.add(alias)
        slots.add(slot)
        prepared.append((slot, alias, frozenset(users)))
    for slot, alias, users in prepared:
        ctx.register_platform(
            name=f'telegram_{alias}', label=f'Telegram {alias}',
            adapter_factory=partial(MultiBotAdapter, slot=slot, alias=alias, allowed_users=users),
            check_fn=lambda: True, config_connected_fn=lambda config: True,
            max_message_length=4096,
        )
