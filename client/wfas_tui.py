#!/usr/bin/env python3
"""
WFAS Native Client - basit terminal durum göstergesi (TUI).

http://localhost:<port>/status.json adresini periyodik olarak okuyup
bağlantı durumunu terminalde renkli şekilde gösterir. Servisin kendisini
BAŞLATMAZ / DURDURMAZ, sadece durumunu izler; asıl istemci systemd --user
üzerinden ya da wfas-launch.sh ile arka planda ayrıca çalışır.

Kullanım:
    python3 wfas_tui.py                 # varsayılan port 8091
    python3 wfas_tui.py --port 9000
"""
import argparse
import curses
import json
import urllib.error
import urllib.request

POLL_INTERVAL = 1.0

def fetch_status(url):
    try:
        with urllib.request.urlopen(url, timeout=1.5) as r:
            return json.loads(r.read().decode("utf-8")), None
    except Exception as e:
        return None, str(e)

def draw(stdscr, url):
    curses.curs_set(0)
    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(1, curses.COLOR_GREEN, -1)
    curses.init_pair(2, curses.COLOR_RED, -1)
    curses.init_pair(3, curses.COLOR_YELLOW, -1)
    stdscr.nodelay(True)
    stdscr.timeout(int(POLL_INTERVAL * 1000))

    while True:
        ch = stdscr.getch()
        if ch in (ord("q"), ord("Q")):
            break

        status, err = fetch_status(url)
        stdscr.erase()
        h, w = stdscr.getmaxyx()

        title = " WFAS Native Client "
        stdscr.addstr(0, max(0, (w - len(title)) // 2), title, curses.A_BOLD)

        row = 2
        if status is None:
            stdscr.addstr(row, 2, "Servise ulaşılamıyor.", curses.color_pair(2) | curses.A_BOLD)
            row += 1
            stdscr.addstr(row, 2, f"({err})"[: max(0, w - 4)])
            row += 2
            stdscr.addstr(row, 2, "Servisi kontrol et:")
            row += 1
            stdscr.addstr(row, 2, "systemctl --user status wfas-native-client")
            row += 1
            stdscr.addstr(row, 2, "veya: pgrep -fa wfas_native_client.py")
        else:
            phase = status.get("phase", "?")
            color = (
                curses.color_pair(1) if phase == "LIVE"
                else curses.color_pair(2) if phase == "ERROR"
                else curses.color_pair(3)
            )
            stdscr.addstr(row, 2, "Durum:      ")
            stdscr.addstr(phase, color | curses.A_BOLD)
            row += 1
            stdscr.addstr(row, 2, f"Sunucu:     {status.get('server') or '-'}")
            row += 1
            sr, chn = status.get("sr"), status.get("ch")
            fmt = f"{sr} Hz / {chn} kanal" if sr and chn else "-"
            stdscr.addstr(row, 2, f"Format:     {fmt}")
            row += 1
            stdscr.addstr(row, 2, f"Paket:      {status.get('packets', 0)}")
            row += 1
            stdscr.addstr(row, 2, f"Sessizlik:  {status.get('silence_inserted', 0)} örnek")
            row += 1
            err_msg = status.get("last_error") or "-"
            stdscr.addstr(row, 2, f"Son hata:   {err_msg}"[: max(0, w - 4)])

        row += 2
        stdscr.addstr(row, 2, "[q] çıkış (servis arka planda çalışmaya devam eder)", curses.A_DIM)
        stdscr.refresh()

def main():
    ap = argparse.ArgumentParser(description="WFAS native client - durum TUI")
    ap.add_argument("--port", type=int, default=8091, help="Durum arayüzü portu (varsayılan 8091)")
    args = ap.parse_args()
    url = f"http://localhost:{args.port}/status.json"
    try:
        curses.wrapper(draw, url)
    except KeyboardInterrupt:
        pass

if __name__ == "__main__":
    main()
