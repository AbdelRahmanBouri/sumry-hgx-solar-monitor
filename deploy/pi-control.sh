#!/bin/sh
# Scoped privileged helper for the Solar Monitor admin portal.
#
# This script is the ONLY thing the service account may run as root (via a
# narrow NOPASSWD sudoers rule -- see solar-monitor-sudoers.example). It never
# evaluates its arguments as shell, and it validates every value before
# acting, so granting sudo access to *this script* is far narrower than
# granting sudo access to systemctl/nmcli/tee directly.
#
# Usage: pi-control.sh <action> [args...]
set -eu

action="${1:-}"
shift || true

case "$action" in
  reboot)
    exec /sbin/reboot
    ;;

  restart-service)
    exec /bin/systemctl restart solar-monitor
    ;;

  governor)
    mode="${1:-}"
    case "$mode" in
      powersave|ondemand|performance|schedutil) ;;
      *) echo "Unsupported governor: $mode" >&2; exit 1 ;;
    esac
    for governor_file in /sys/devices/system/cpu/cpu[0-9]*/cpufreq/scaling_governor; do
      [ -w "$governor_file" ] && echo "$mode" > "$governor_file"
    done
    echo "Governor set to $mode"
    ;;

  wifi-scan)
    exec /usr/bin/nmcli -t -f ssid,signal,security device wifi list --rescan yes
    ;;

  wifi-connect)
    ssid="${1:-}"
    password="${2:-}"
    if [ -z "$ssid" ]; then
      echo "Missing SSID" >&2
      exit 1
    fi
    exec /usr/bin/nmcli device wifi connect "$ssid" password "$password"
    ;;

  *)
    echo "Unknown action: $action" >&2
    exit 2
    ;;
esac
