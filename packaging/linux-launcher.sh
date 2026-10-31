#!/bin/sh
# Copyright (C) 2026 Stefano Natangelo
# SPDX-License-Identifier: AGPL-3.0-only

glibc_release=$(PATH="${PATH:-/usr/bin:/bin}" getconf GNU_LIBC_VERSION 2>/dev/null) || glibc_release=
version=
case "$glibc_release" in
  'glibc '*) version=${glibc_release#glibc } ;;
esac

case "$version" in
  ''|*[!0-9.]*|.*|*.) version= ;;
  *.*) ;;
  *) version= ;;
esac

major=${version%%.*}
minor=${version#*.}
minor=${minor%%.*}
case "$major:$minor" in
  *[!0-9:]*|:|*:) version= ;;
esac

if [ -n "$version" ]; then
  while [ "${major#0}" != "$major" ]; do major=${major#0}; done
  while [ "${minor#0}" != "$minor" ]; do minor=${minor#0}; done
  [ -n "$major" ] || major=0
  [ -n "$minor" ] || minor=0
fi

supported=false
if [ -n "$version" ]; then
  if [ "${#major}" -gt 1 ] || [ "$major" -gt 2 ]; then
    supported=true
  elif [ "$major" -eq 2 ]; then
    if [ "${#minor}" -gt 2 ]; then
      supported=true
    elif [ "${#minor}" -eq 2 ] && [ "$minor" -ge 35 ]; then
      supported=true
    fi
  fi
fi

if [ "$supported" != true ]; then
  if [ -n "$version" ]; then
    message="Callimachus requires GNU glibc 2.35 or later; detected GNU glibc $major.$minor."
  else
    message="Callimachus requires GNU glibc 2.35 or later; this system's GNU glibc version could not be determined."
  fi
  printf '%s\n' "$message" >&2
  dialog_shown=false
  if command -v zenity >/dev/null 2>&1; then
    if zenity --error --title=Callimachus --text="$message" >/dev/null 2>&1; then
      dialog_shown=true
    fi
  fi
  if [ "$dialog_shown" != true ] && command -v kdialog >/dev/null 2>&1; then
    kdialog --error "$message" --title=Callimachus >/dev/null 2>&1 || true
  fi
  exit 2
fi

case "$0" in
  /*) launcher_path=$0 ;;
  */*) launcher_path=$PWD/$0 ;;
  *) launcher_path=$(command -v "$0" 2>/dev/null) || launcher_path= ;;
esac
if [ -z "$launcher_path" ]; then
  printf '%s\n' "Callimachus could not locate its public launcher." >&2
  exit 2
fi

case "$launcher_path" in
  /*) ;;
  *) launcher_path=$PWD/$launcher_path ;;
esac

link_count=0
while [ -L "$launcher_path" ]; do
  link_count=$((link_count + 1))
  if [ "$link_count" -gt 40 ]; then
    printf '%s\n' "Callimachus could not resolve its public launcher symlink chain." >&2
    exit 2
  fi
  link_target=$(PATH="${PATH:-/usr/bin:/bin}" readlink "$launcher_path" 2>/dev/null) || link_target=
  if [ -z "$link_target" ]; then
    printf '%s\n' "Callimachus could not resolve its public launcher symlink." >&2
    exit 2
  fi
  case "$link_target" in
    /*) launcher_path=$link_target ;;
    *) launcher_path=${launcher_path%/*}/$link_target ;;
  esac
done

launcher_dir=${launcher_path%/*}
[ -n "$launcher_dir" ] || launcher_dir=/
launcher_dir=$(CDPATH= cd -P "$launcher_dir" 2>/dev/null && pwd -P) || {
  printf '%s\n' "Callimachus could not resolve the directory of its public launcher." >&2
  exit 2
}

exec "$launcher_dir/Callimachus.bin" "$@"
