"""Command-line interface for controlling the TRYX Panorama AIO display.

Talks to the background daemon (see ``tryx.daemon``), which owns the serial
connection and keeps the display alive after this command exits.
"""

from __future__ import annotations

import argparse
import json
import sys

from . import __version__
from .client import TryxClient
from .device import detect_device, DEFAULT_PORT


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="tryxctl",
        description="Change the image/video shown on a TRYX Panorama AIO "
                    "display (Rockchip cm01_se).",
    )
    p.add_argument("--port", default=DEFAULT_PORT,
                   help=f"serial device used when starting the daemon "
                        f"(default: {DEFAULT_PORT})")
    p.add_argument("--baud", type=int, default=115200, help="baud rate")
    p.add_argument("--no-adb", action="store_true",
                   help="use the serial file-transfer protocol instead of adb")
    p.add_argument("--version", action="version",
                   version=f"%(prog)s {__version__}")

    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("info", help="show device info")
    sub.add_parser("scan", help="detect the AIO on USB (no connection needed)")

    for name, help_text in (("image", "show a single static image"),
                            ("video", "show a looping video")):
        sp = sub.add_parser(name, help=help_text)
        sp.add_argument("file", help="local image/video file")
        sp.add_argument("--ratio", default="1:1",
                        help="aspect ratio: '' | 1:1 | 3:2 | 4:3 | 16:9 "
                             "(default: 1:1)")

    sp = sub.add_parser("slideshow", help="cycle through multiple images")
    sp.add_argument("files", nargs="+", help="local image files")
    sp.add_argument("--ratio", default="1:1",
                    help="aspect ratio: '' | 1:1 | 3:2 | 4:3 | 16:9 "
                         "(default: 1:1)")

    sp = sub.add_parser("sysinfo", help="switch to hardware-monitoring screen")
    sp.add_argument("--items", nargs="*", default=None,
                    help="stats to show (e.g. 'Cpu Usage' 'Gpu Usage')")

    sp = sub.add_parser("delete", help="delete media from the device")
    g = sp.add_mutually_exclusive_group(required=True)
    g.add_argument("--include", nargs="+", help="files to delete")
    g.add_argument("--exclude", nargs="+", help="files to keep")
    g.add_argument("--all", action="store_true", help="delete all media")

    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)

    if args.command == "scan":
        return _run_scan()

    try:
        with TryxClient(args.port, args.baud) as client:
            return _run(client, args)
    except (ConnectionError, ConnectionRefusedError, FileNotFoundError) as e:
        print(f"error: {e}", file=sys.stderr)
        print("hint: the AIO did not respond. Run 'tryxctl scan'; if it is "
              "missing, fully power off the PC (PSU off/unplug, 30 s) to "
              "reset the display board.", file=sys.stderr)
        return 1


def _run(client: TryxClient, args: argparse.Namespace) -> int:
    adb = not args.no_adb

    if args.command == "info":
        print(json.dumps(client.info(), indent=2, ensure_ascii=False))
        return 0

    if args.command == "image":
        r = client.set_image(args.file, ratio=args.ratio, use_adb=adb)
        what = args.file
    elif args.command == "video":
        r = client.set_video(args.file, ratio=args.ratio, use_adb=adb)
        what = args.file
    elif args.command == "slideshow":
        r = client.set_slideshow(list(args.files), ratio=args.ratio, use_adb=adb)
        what = f"{len(args.files)} images"
    elif args.command == "sysinfo":
        r = client.set_sysinfo(args.items)
        what = "hardware monitor"
    elif args.command == "delete":
        if args.all:
            r = client.delete_media(exclude=[])
        elif args.include:
            r = client.delete_media(include=args.include)
        else:
            r = client.delete_media(exclude=args.exclude)
        what = "deleted media"
    else:
        return 0

    if r.get("ok"):
        print(f"done: {what}")
        return 0
    print(f"error: {r.get('error', 'unknown error')}", file=sys.stderr)
    return 1


def _run_scan() -> int:
    info = detect_device()
    if info["present"]:
        print(f"AIO found on USB: {info['lsusb']}")
    else:
        print("AIO NOT found on USB (expected 18d1:2d04 / rockchip cm01_se).")
    ports = info["serial_ports"]
    print(f"Serial ports: {', '.join(ports) if ports else 'none'}")
    adb = info["adb_devices"]
    print(f"ADB devices:  {adb if adb else 'none'}")
    if not info["present"] and not ports:
        print("\n-> The display board appears to be powered off or hung. "
              "Fully power off the PC (shutdown, then switch off/unplug the "
              "PSU), wait ~30 s, and power back on. A normal 'restart' does "
              "NOT cut power to the AIO.")
    return 0 if info["present"] else 1


if __name__ == "__main__":
    sys.exit(main())
