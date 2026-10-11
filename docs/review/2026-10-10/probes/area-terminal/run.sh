#!/bin/bash
# usage: run.sh <label> <args...>   -- runs python -m ixel_mat in an isolated home
T=/tmp/claude-0/-home-user-Ixel-MAT/c585261b-b9d6-5506-884a-19d4ae6ba125/scratchpad/area-terminal/home
mkdir -p "$T/.config"
label="$1"; shift
echo "=================== [$label] ixel $* ==================="
cd "$T" && env -i PATH="$PATH" HOME="$T" XDG_CONFIG_HOME="$T/.config" IXEL_TEST_KEYCHAIN=memory IXEL_NO_UPDATE_CHECK=1 PYTHONIOENCODING=utf-8 PYTHONPATH=/home/user/Ixel-MAT TERM=xterm-256color timeout 60 python -m ixel_mat "$@" </dev/null
echo "--- exit: $? ---"
