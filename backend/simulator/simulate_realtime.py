#!/usr/bin/env python3
"""Simulate real-time incident ingestion — sends tickets to /classify
   at configurable intervals, mimicking a real ticketing system integration.

Usage:
  python3 simulate_realtime.py                    # default: 30s interval
  python3 simulate_realtime.py --interval 10       # every 10 seconds
  python3 simulate_realtime.py --file /tmp/tickets.json
  python3 simulate_realtime.py --loop             # repeat forever
"""

import json
import time
import urllib.request
import urllib.error
import argparse
import sys
import random
from datetime import datetime

API = "http://localhost:8000"

def classify(title, description):
    payload = json.dumps({"title": title, "description": description}).encode()
    req = urllib.request.Request(
        f"{API}/classify", data=payload,
        headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=180) as resp:
        return json.loads(resp.read())

def main():
    parser = argparse.ArgumentParser(description="Simulate real-time incident ingestion")
    parser.add_argument("--interval", type=int, default=30, help="Seconds between incidents (default: 30)")
    parser.add_argument("--file", default="/tmp/test_tickets.json", help="JSON file with incident list")
    parser.add_argument("--loop", action="store_true", help="Loop forever when reaching end of list")
    parser.add_argument("--shuffle", action="store_true", help="Send incidents in random order")
    args = parser.parse_args()

    with open(args.file) as f:
        all_tickets = json.load(f)

    print(f"🚀 Starting real-time ingestion simulation")
    print(f"   API: {API}")
    print(f"   Tickets: {len(all_tickets)}")
    print(f"   Interval: {args.interval}s")
    print(f"   Loop: {'yes' if args.loop else 'no'}")
    print(f"   Shuffle: {'yes' if args.shuffle else 'no'}")
    print(f"{'─' * 60}")

    idx = 0
    tickets = all_tickets[:]
    if args.shuffle:
        random.shuffle(tickets)

    while True:
        if idx >= len(tickets):
            if not args.loop:
                print(f"\n✅ All {len(tickets)} incidents sent. Use --loop to repeat.")
                break
            print(f"\n🔄 Reached end, restarting...\n")
            idx = 0
            if args.shuffle:
                random.shuffle(tickets)

        t = tickets[idx]
        ts = datetime.now().strftime("%H:%M:%S")
        title_short = t["title"][:55]

        sys.stdout.write(f"[{ts}] Sending ({idx+1}/{len(tickets)}): {title_short}... ")
        sys.stdout.flush()

        try:
            result = classify(t["title"], t["description"])
            c = result["classification"]
            dupes = result.get("similar_open_incidents", [])
            sys.stdout.write(f"✅ {c['affected_system']}/{c['incident_type']} "
                           f"[{c['confidence']}] dupes={len(dupes)}\n")
            sys.stdout.flush()
        except Exception as e:
            print(f"❌ {str(e)[:60]}")

        idx += 1

        if idx < len(tickets) or args.loop:
            time.sleep(args.interval)

if __name__ == "__main__":
    main()
