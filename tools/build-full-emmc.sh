#!/usr/bin/env bash
# build-full-emmc.sh -- GPT eMMC image with the FULL vendor by-name layout the
# head-unit recovery fstab expects (adds navi/navi2/upgrade/vr1/vr2/oem_data that
# a lean build omits), optionally seeded with vendor firmware partitions.
# Purpose: give the *stock* /sbin/recovery every partition its update flow
# touches, so it can run end-to-end inside the emulator.
#
# Every path is passed on the command line or via the environment. No absolute
# path is compiled in, so the script runs from any checkout.
#
# Usage:
#   build-full-emmc.sh --system system.ext4 --cache cache.img --data userdata.img \
#                      --recovery recovery.img --out emmc-full.img
#
#   build-full-emmc.sh --out emmc-layout.img          # partitions only, no content
#   build-full-emmc.sh --help
#
# A source that is omitted leaves that partition as a fresh empty ext4; --strict
# turns that into an error so the old "all four present" behaviour is available.

set -eu

read -r -d '' USAGE <<'EOF' || true
usage: build-full-emmc.sh [options]

Options:
  --system FILE     vendor system.ext4       -> seeded into 'system'
  --cache  FILE     vendor cache image       -> seeded into 'cache'
  --data   FILE     vendor userdata image    -> seeded into 'data'
  --recovery FILE   stock recovery bootimg   -> seeded into 'recovery'
  --out    FILE     output image             (default: emmc-full.img)
  --strict          fail if any of the four sources above is missing
  --force           overwrite an existing output image
  --help            show this text

Builds a GPT eMMC image with the full vendor by-name layout
(boot recovery misc kpanic log system cache data securedata navi navi2
upgrade vr1 vr2 oem_data) ready for the emulator.

Every path is passed on the command line, so the script runs from any
checkout. A source that is omitted leaves that partition as a fresh empty
ext4; --strict turns that into an error. boot, misc, kpanic and log are
created but left blank, as before.
EOF

PROGNAME=${0##*/}
die() { printf '%s: %s\n' "$PROGNAME" "$*" >&2; exit 1; }
note() { printf '=== %s\n' "$*" >&2; }

usage() { printf '%s\n' "$USAGE"; exit "${1:-0}"; }

# ----------------------------------------------------------------- arguments
OUT=emmc-full.img
SYS=; CACHE=; DATA=; RECIMG=
STRICT=0; FORCE=0

[ $# -gt 0 ] || usage 1
while [ $# -gt 0 ]; do
    case $1 in
        --out)       OUT=${2:?--out needs a value}; shift 2 ;;
        --system)    SYS=${2:?--system needs a value}; shift 2 ;;
        --cache)     CACHE=${2:?--cache needs a value}; shift 2 ;;
        --data)      DATA=${2:?--data needs a value}; shift 2 ;;
        --recovery)  RECIMG=${2:?--recovery needs a value}; shift 2 ;;
        --strict)    STRICT=1; shift ;;
        --force)     FORCE=1; shift ;;
        -h|--help)   usage 0 ;;
        # --opt=value is accepted as well as "--opt value"
        --out=*)       OUT=${1#*=}; shift ;;
        --system=*)    SYS=${1#*=}; shift ;;
        --cache=*)     CACHE=${1#*=}; shift ;;
        --data=*)      DATA=${1#*=}; shift ;;
        --recovery=*)  RECIMG=${1#*=}; shift ;;
        *) die "unknown argument '$1' (try --help)" ;;
    esac
done
OUT=${OUT:?no output file given}
if [ "$STRICT" = 1 ]; then
    for pair in "system:$SYS" "cache:$CACHE" "data:$DATA" "recovery:$RECIMG"; do
        [ -n "${pair#*:}" ] || die "--strict requires --${pair%%:*} to be given"
    done
fi
# resolve to absolute paths early so a later cd cannot change their meaning
toabs() { case $1 in /*) printf '%s\n' "$1";; *) printf '%s\n' "$PWD/$1";; esac; }
SYS=${SYS:+$(toabs "$SYS")};      CACHE=${CACHE:+$(toabs "$CACHE")}
DATA=${DATA:+$(toabs "$DATA")};   RECIMG=${RECIMG:+$(toabs "$RECIMG")}
OUT=$(toabs "$OUT")
mkdir -p -- "$(dirname -- "$OUT")"

for pair in "system:$SYS" "cache:$CACHE" "data:$DATA" "recovery:$RECIMG"; do
    p=${pair#*:}
    [ -z "$p" ] && continue
    [ -f "$p" ] || die "source for ${pair%%:*} is not a regular file: $p"
    [ -r "$p" ] || die "source for ${pair%%:*} is not readable: $p"
done

for tool in sgdisk mke2fs dd truncate; do
    command -v "$tool" >/dev/null 2>&1 || die "required tool not found: $tool"
done

if [ -e "$OUT" ]; then
    [ "$FORCE" = 1 ] || die "output already exists: $OUT (use --force to overwrite)"
    note "overwriting $OUT"
fi

# ------------------------------------------------------- by-name partition table
# name -> size in MiB (names match the recovery fstab's by-name entries)
ORDER="boot recovery misc kpanic log system cache data securedata navi navi2 upgrade vr1 vr2 oem_data"
declare -A size=(
  [boot]=16 [recovery]=32 [misc]=1 [kpanic]=8 [log]=16 [system]=2048
  [cache]=278 [data]=520 [securedata]=16 [navi]=512 [navi2]=512
  [upgrade]=512 [vr1]=64 [vr2]=64 [oem_data]=64
)
# 16 MiB covers the 1 MiB leading offset plus GPT primary/secondary tables and
# the alignment slack between them.
sum=0; for n in $ORDER; do sum=$(( sum + ${size[$n]} )); done
total_mib=$(( sum + 16 ))
last_usable=$(( total_mib * 2048 - 34 ))

# start sector per name, contiguous from 1 MiB, with exact inclusive end sectors
declare -A start end_sector
sec=2048
for n in $ORDER; do
    start[$n]=$sec
    end_sector[$n]=$(( sec + ${size[$n]} * 2048 - 1 ))
    sec=$(( ${end_sector[$n]} + 1 ))
done
[ "${end_sector[${ORDER##* }]}" -le "$last_usable" ] || die "layout exceeds image"

note "allocating ${total_mib} MiB: $OUT"
rm -f -- "$OUT"
truncate -s ${total_mib}M "$OUT"

# ------------------------------------------------------------------- create GPT
note "sgdisk: 15 named partitions (exact sector ends, no re-alignment)"
sgdisk_args=( --clear -a 1 )
i=0
for n in $ORDER; do
    i=$(( i + 1 ))
    sgdisk_args+=( --new=$i:${start[$n]}:${end_sector[$n]}
                   --change-name=$i:"$n"
                   --typecode=$i:0FC63DAF-8483-4772-8E79-3D69D8477DE4 )
done
sgdisk "${sgdisk_args[@]}" "$OUT" >/dev/null

# ---------------------------------------------------------------------- seeding
seeded=(); emptied=(); blank="boot misc kpanic log"

# byte offset of a partition's first sector
sector_bytes() { echo $(( $1 * 512 )); }

seed_dd() {   # name file -- copy vendor content into the partition
    local n=$1 f=$2 psz=$(( ${size[$n]} * 1024 * 1024 )) fsz
    fsz=$(stat -c %s -- "$f")
    [ "$fsz" -le "$psz" ] || die "$n source is ${fsz} B but partition is ${psz} B: $f"
    note "seed[$n] <= $f (@${start[$n]} sectors, ${fsz} B)"
    dd if="$f" of="$OUT" bs=1M seek=$(( ${start[$n]} / 2048 )) \
       conv=notrunc,sparse status=none
    seeded+=("$n")
}

seed_mkfs() { # name -- fresh empty ext4 written in place at the partition offset
    local n=$1 off blocks
    off=$(sector_bytes "${start[$n]}")
    blocks=$(( ${size[$n]} * 1024 ))
    note "mkfs[$n] in place at $off B, $blocks blocks"
    mke2fs -q -t ext4 -b 1024 -m 0 -O ^64bit,^metadata_csum \
           -E offset=$off "$OUT" "$blocks"
    emptied+=("$n")
}

note "seeding vendor content"
# Data-bearing partitions: seeded when a source is given, empty ext4 otherwise.
for spec in "system:$SYS" "cache:$CACHE" "data:$DATA"; do
    n=${spec%%:*}; src=${spec#*:}
    if [ -n "$src" ]; then seed_dd "$n" "$src"; else seed_mkfs "$n"; fi
done
# The recovery partition receives the stock recovery boot image so by-name is
# populated. Failure is tolerated (as before) because a recovery image is not
# always available; everything else still builds.
if [ -n "$RECIMG" ]; then
    note "seed[recovery] <= $RECIMG"
    dd if="$RECIMG" of="$OUT" bs=512 seek=${start[recovery]} \
       conv=notrunc status=none || note "recovery seed failed, continuing"
    seeded+=("recovery")
fi
# The remaining empty partitions just need to exist and be mountable.
for n in securedata navi navi2 upgrade vr1 vr2 oem_data; do seed_mkfs "$n"; done

# ------------------------------------------------------------------------ report
note "done: $OUT"
sgdisk -p "$OUT" | sed -n '/^Number/,$p'
printf 'components: seeded[%s] empty-ext4[%s] blank[%s]\n' \
       "${seeded[*]:-none}" "${emptied[*]:-none}" "$blank" >&2
printf 'image: %s  virtual-size=%s bytes\n' "$OUT" \
       "$(stat -c %s -- "$OUT")" >&2
