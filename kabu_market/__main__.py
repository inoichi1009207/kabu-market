"""Play in a terminal: ``python -m kabu_market [save.sqlite3]``.

Type the same commands as in a group, e.g. ``/kabu 签到``. Extra local commands:
``:user NAME`` switch player, ``:@ NAME`` mention someone in the next message,
``:wait 12h`` / ``:wait 3d`` move the clock forward, ``:quit`` leave.
"""

import re
import sys

from . import Event, FakeClock, KabuMarket, Scope, Store


def main(argv):
    store = Store(argv[1] if len(argv) > 1 else ":memory:")
    clock = FakeClock(__import__("time").time())
    market = KabuMarket(store, clock)
    scope = Scope("local-bot", "group", "local-group")
    user, mention = "player", ()
    print(
        "kabu-market 本地试玩。发 /kabu 看玩法；:user 名字 换人，:@ 名字 下一句 @ 谁，:wait 12h 快进，:quit 退出。"
    )
    while True:
        try:
            line = input(f"[{user}] ").strip()
        except EOFError:
            break
        if not line:
            continue
        if line == ":quit":
            break
        if line.startswith(":user "):
            user = line.split(maxsplit=1)[1]
            continue
        if line.startswith(":@ "):
            mention = (line.split(maxsplit=1)[1],)
            continue
        wait = re.fullmatch(r":wait (\d+)([hd])", line)
        if wait:
            clock.advance(int(wait.group(1)) * (3600 if wait.group(2) == "h" else 86400))
            print("（时间快进了）")
            continue
        if not KabuMarket.matches(line):
            print("（只认 /kabu 开头的命令）")
            continue
        event = Event(scope, user, line, sender_name=user, mentions=mention)
        mention = ()
        print(market.respond(event))
    store.close()


if __name__ == "__main__":
    main(sys.argv)
