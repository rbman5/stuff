#!/usr/bin/env bash
# patch_warning.sh
# Source this from your ~/.bash_profile or ~/.bashrc:
#   source /path/to/patch_warning.sh

_patch_warning() {
    local today_epoch today_ymd year month
    local first_day_dow second_sat_dom second_sat_epoch
    local days_until warning_start

    # Today's date components
    today_ymd=$(date '+%Y-%m-%d')
    year=$(date '+%Y')
    month=$(date '+%m')

    # Epoch for today (portable: works on Linux & macOS)
    if date -d "$today_ymd" '+%s' &>/dev/null 2>&1; then
        today_epoch=$(date -d "$today_ymd" '+%s')                     # Linux / GNU date
    else
        today_epoch=$(date -j -f '%Y-%m-%d' "$today_ymd" '+%s')      # macOS / BSD date
    fi

    # Day-of-week for the 1st of this month (0=Sun … 6=Sat)
    if date -d "${year}-${month}-01" '+%w' &>/dev/null 2>&1; then
        first_day_dow=$(date -d "${year}-${month}-01" '+%w')
    else
        first_day_dow=$(date -j -f '%Y-%m-%d' "${year}-${month}-01" '+%w')
    fi

    # Day-of-month for the 2nd Saturday
    # Days until first Saturday from day 1:  (6 - first_day_dow) % 7
    # Then add 7 for the 2nd Saturday, plus 1 for 1-based month day
    local offset=$(( (6 - first_day_dow) % 7 ))
    second_sat_dom=$(( 1 + offset + 7 ))
    local second_sat_ymd
    second_sat_ymd=$(printf '%s-%s-%02d' "$year" "$month" "$second_sat_dom")

    # Epoch for the 2nd Saturday
    if date -d "$second_sat_ymd" '+%s' &>/dev/null 2>&1; then
        second_sat_epoch=$(date -d "$second_sat_ymd" '+%s')
    else
        second_sat_epoch=$(date -j -f '%Y-%m-%d' "$second_sat_ymd" '+%s')
    fi

    days_until=$(( (second_sat_epoch - today_epoch) / 86400 ))
    warning_start=7   # start warning this many days before patch day

    # Only warn if patch day is in the future and within the warning window
    if (( days_until >= 0 && days_until <= warning_start )); then
        local RED='\033[0;31m'
        local YELLOW='\033[1;33m'
        local BOLD='\033[1m'
        local RESET='\033[0m'

        echo ""
        if (( days_until == 0 )); then
            echo -e "${RED}${BOLD}╔══════════════════════════════════════════════════════╗${RESET}"
            echo -e "${RED}${BOLD}║  ⚠  PATCH DAY — THIS HOST IS BEING PATCHED TODAY  ⚠  ║${RESET}"
            echo -e "${RED}${BOLD}╚══════════════════════════════════════════════════════╝${RESET}"
            echo -e "${RED}  Scheduled maintenance is today: ${second_sat_ymd}${RESET}"
            echo -e "${RED}  Save your work and expect possible downtime.${RESET}"
        elif (( days_until == 1 )); then
            echo -e "${RED}${BOLD}⚠  PATCH WARNING: This host will be patched TOMORROW (${second_sat_ymd})${RESET}"
            echo -e "${RED}  Save your work and wrap up any long-running processes.${RESET}"
        else
	    if [ -f "$HOME/.acknowledge_patches" ]; then
                return
            fi
            echo -e "${YELLOW}${BOLD}⚠  PATCH WARNING: This host will be patched in ${days_until} days (${second_sat_ymd})${RESET}"
            echo -e "${YELLOW}  Plan accordingly and save any important work.${RESET}"
	    echo -e "${YELLOW}  Run ${BOLD}touch ~/.acknowledge_patches ${RESET}${YELLOW}to hide this message ${RESET}"
	    echo -e "${YELLOW}  You accept no more reminders for patches except the day before and day of ${RESET}"
        fi
        echo ""

    fi
}

_patch_warning
