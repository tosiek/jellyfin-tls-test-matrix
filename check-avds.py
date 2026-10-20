#!/usr/bin/env python3
"""Compares every setting of the TLS_* AVDs (config.ini and the .ini next to it) of this machine with the remote machines
(after ./setup.sh on each). Nothing printed per AVD = the same. Spelling differences are not differences: "6G" and
"6442450944", "yes" and "YES", spaces around "=". Not compared: userdata, hardware-qemu.ini (written by the emulator).
  ./check-avds.py                      # user@remote-c, remote-a, remote-b
  ./check-avds.py user@remote-a
  ./check-avds.py -i hw.cpu.ncore      # also tolerate the difference of a key (repeatable)
"""
import argparse, os, re, subprocess, sys

AVDS = ["TLS_API_23", "TLS_API_24", "TLS_API_26", "TLS_API_28", "TLS_API_29", "TLS_API_30", "TLS_API_33", "TLS_API_34",
        "TLS_API_36", "TLS_API_37", "TLS_TV_30", "TLS_TV_34"]
AVD_HOME = os.environ.get("ANDROID_AVD_HOME", os.path.expanduser("~/.android/avd"))
# Settings that differ on purpose per machine, checked against this value and not against the one of this machine, e.g.
# {"remote-a": {"hw.cpu.ncore": "2"}} (./setup.sh with AVD_CORES=2). None: 4 virtual CPUs everywhere
PER_MACHINE = {}
UNITS = {"": 1, "K": 1 << 10, "KB": 1 << 10, "M": 1 << 20, "MB": 1 << 20, "G": 1 << 30, "GB": 1 << 30}


def parse(text, prefix=""):
    return {prefix + m[1]: m[2].strip() for m in (re.match(r"\s*([\w.+-]+)\s*=\s*(.*)$", line) for line in text.splitlines()) if m}


def normal(value):
    """Sizes in bytes (a number with an optional K, M or G), yes/no in lower case."""
    m = re.fullmatch(r"(\d+)\s*([KMG]B?)?", value, re.I)
    if m and (m[2] or len(m[1]) > 6):                       # 6G, 512 MB, 6442450944; not 4, 60, 1080
        return f"{int(m[1]) * UNITS[(m[2] or '').upper()]}B"
    return value.lower() if value.lower() in ("yes", "no", "true", "false") else value


def local(avd):
    texts = []
    for name in (f"{avd}.avd/config.ini", f"{avd}.ini"):
        try:
            texts.append(open(f"{AVD_HOME}/{name}").read())
        except OSError:
            texts.append("")
    return parse(texts[0]), parse(texts[1], "ini:")


def remote(host, avd):
    out = subprocess.run(["ssh", "-o", "BatchMode=yes", host,
                          f"cat ~/.android/avd/{avd}.avd/config.ini; echo @@INI@@; cat ~/.android/avd/{avd}.ini"],
                         capture_output=True, text=True).stdout
    config, _, ini = out.partition("@@INI@@")
    return parse(config), parse(ini, "ini:")


parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
parser.add_argument("hosts", nargs="*")
parser.add_argument("-i", "--ignore", action="append", default=[], help="key whose difference is tolerated")
args = parser.parse_args()
hosts = args.hosts or [f"user@remote-{n}" for n in ("c", "a", "b")]
different = 0
for avd in AVDS:
    mine = {**local(avd)[0], **local(avd)[1]}
    for host in hosts:
        theirs = {**remote(host, avd)[0], **remote(host, avd)[1]}
        expected = {**mine, **PER_MACHINE.get(host.split("@")[-1], {})}
        diff = [f"{k}: expected={expected.get(k, '(none)')} there={theirs.get(k, '(none)')}"
                for k in sorted(set(expected) | set(theirs))
                if k not in args.ignore and normal(expected.get(k, "(none)")) != normal(theirs.get(k, "(none)"))]
        if diff:
            different += 1
            print(f"{avd} {host}:\n  " + "\n  ".join(diff))
print(f"{len(AVDS)} AVDs x {len(hosts)} machines compared (every setting), {different} differ")
sys.exit(1 if different else 0)
