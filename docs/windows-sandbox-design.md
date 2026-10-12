# The Windows command sandbox

When the Agent runs an approved command on Windows, it runs it with a **low-integrity token inside a job
object**. This note says what that does, the Win32 calls in order, what it does **not** do, what happens when
it can't be set up, the risks, and how to test it on a real Windows machine.

**Status.** The code is written: `ixel_mat/agent/winsandbox.py` (the sandbox), `ixel_mat/agent/sandbox.py`
(`sandbox_kind`, `CommandResult.sandbox`, and `run_command` using it), the approval card in `agent.js`, and
tests in `tests/test_agent_winsandbox.py`. It was written and reviewed without a Windows machine, so **it has
not been run on Windows yet**. The test plan at the end is what has to happen before anyone relies on it.

## In one paragraph

A command on Windows can write only in the workspace and in a private temp folder. The network is **on**, and
reads are **not limited**: the command can read any file your account can, including your SSH, cloud and
browser folders and Ixel's own config folder, and print it into the run. Windows can't hide those folders from
it the way Linux does (`bwrap`). That's why the approval card says **Partly sandboxed (Windows)**, and says so in
the same words every time.

## What it does, and why

- **Low integrity.** Every Windows process has an integrity level: Low, Medium (what you normally run at),
  High (administrator) and System. A process can't *write* to a file, folder or registry key that is labelled
  with a higher level (the "no write up" rule). Almost everything in your user profile is Medium, so a Low
  process can't change it. The command gets a copy of Ixel's own token with its level lowered to Low
  (`S-1-16-4096`). It needs no administrator rights, and nothing is installed.
- **A Low label on the places it may write.** For the run only, the workspace folder and a new private temp
  folder (`%TEMP%\ixel-agent-…`, with `TEMP` and `TMP` pointing at it) get a Low label that new and existing
  files below it inherit: `S:(ML;OICI;NW;;;LW)` (a mandatory label, inherited by files and folders, no write
  up, for the Low level). A Low process may write to a Low object, so the command can write in those two
  folders and nowhere else that is Medium. Ixel and your other programs, running at Medium, still write
  there as usual (writing *down* to a Low object is allowed).
- **A job object.** The command and everything it starts live in one job object with
  `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`. Stop, the time limit and Ixel quitting all end the job, so nothing
  it started is left running.
- **The same run as before.** Output (one pipe for output and errors), the 120-second limit, Stop, the 64 KB
  output cut, the environment without the keys saved in Ixel (`child_env`) and the workspace as its folder
  are all as they are on the other systems.

## The Win32 calls, in order

`winsandbox.start(argv, workspace, env)`. All of it is `ctypes`; the functions are loaded once, by `_api()`.

1. `subprocess.list2cmdline(argv)` makes the one command line `CreateProcess` takes (at most 32,766
   characters). A whole drive can't be the workspace (it would mean labelling every file on it).
2. `tempfile.mkdtemp(prefix="ixel-agent-")` makes the private temp folder. The environment is `env` with
   `TEMP` and `TMP` replaced (any case) by it, as a Unicode block.
3. **Label the workspace** (`_hold_folder`):
   `GetNamedSecurityInfoW(path, SE_FILE_OBJECT, LABEL_SECURITY_INFORMATION)` and
   `ConvertSecurityDescriptorToStringSecurityDescriptorW` read its label as an SDDL string, which is kept. If it
   is already a Low, inheritable, no-write-up label, it is left alone and never restored. Otherwise
   `ConvertStringSecurityDescriptorToSecurityDescriptorW("S:(ML;OICI;NW;;;LW)")`, `GetSecurityDescriptorSacl`
   and `SetNamedSecurityInfoW(path, SE_FILE_OBJECT, LABEL_SECURITY_INFORMATION, …, sacl)` set it, and the
   descriptor is freed (`LocalFree`). `SetNamedSecurityInfoW` also applies it to what is already inside.
   Runs that overlap in one folder share the label: it is set by the first and taken off by the last (a count
   under a lock).
4. **Label the temp folder** the same way.
5. **The token** (`_low_token`): `OpenProcessToken(GetCurrentProcess(), TOKEN_DUPLICATE | TOKEN_ADJUST_DEFAULT
   | TOKEN_QUERY | TOKEN_ASSIGN_PRIMARY)`, `DuplicateTokenEx(…, SecurityImpersonation, TokenPrimary)`,
   `ConvertStringSidToSidW("S-1-16-4096")`, `SetTokenInformation(TokenIntegrityLevel,
   TOKEN_MANDATORY_LABEL{sid, SE_GROUP_INTEGRITY})`, `LocalFree(sid)`, and the original token's handle is
   closed.
6. **The job** (`_new_job`): `CreateJobObjectW`, then `SetInformationJobObject(JobObjectExtendedLimitInformation,
   JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE)`.
7. **The pipe and input**: `CreatePipe` with an inheritable `SECURITY_ATTRIBUTES`, then
   `SetHandleInformation(read end, HANDLE_FLAG_INHERIT, 0)` so only the write end is inherited. The read end
   becomes a Python binary file with `msvcrt.open_osfhandle` and `open(fd, "rb")` (it has `read1`, as
   `Popen.stdout` does). `CreateFileW("NUL", GENERIC_READ, …, inheritable)` is its input.
8. **Start it suspended** (`_create_suspended`): `InitializeProcThreadAttributeList` (called twice, to get the
   size), `UpdateProcThreadAttribute(PROC_THREAD_ATTRIBUTE_HANDLE_LIST, [input, output])`, a `STARTUPINFOEXW`
   with `STARTF_USESTDHANDLES` (input NUL; output and errors the pipe), then
   `CreateProcessAsUserW(token, NULL, commandline, NULL, NULL, TRUE, CREATE_SUSPENDED | CREATE_NO_WINDOW |
   CREATE_UNICODE_ENVIRONMENT | EXTENDED_STARTUPINFO_PRESENT, environment, workspace, …)`, and
   `DeleteProcThreadAttributeList`. **The handle list matters:** `bInheritHandles` is TRUE, and without the
   list the Low child would inherit every inheritable handle Ixel holds, some of which can write where a Low
   process must not. With it, the child gets those two handles and no others.
9. `AssignProcessToJobObject(job, process)`, then `ResumeThread(thread)`. If assigning fails, the process is
   ended (`TerminateProcess`) before it ever ran.
10. In the parent, the thread handle, the token, the pipe's write end and the NUL handle are closed.
11. **While it runs:** `WaitForSingleObject(process, 0)` and `GetExitCodeProcess` (`poll`), a timed
    `WaitForSingleObject` (`wait`), and `TerminateJobObject(job, 1)` (`kill`: ends it and everything it
    started).
12. **Afterwards** (`LowProcess.close`, safe to call twice): the process and job handles are closed (closing the
    job ends anything still in it), the temp folder's label is cleared and the folder removed, and the
    workspace's label is put back if this was the last run holding it and we were the ones who set it
    (`SetNamedSecurityInfoW` with the saved SDDL, or an empty label ACL when there was none).

A `CreateProcessAsUserW` failure is mapped as `Popen` would: error 2 or 3 is `FileNotFoundError` ("there's no
program …"), 5 is `PermissionError`, 193 and 216 are `OSError`. Any other failure, and a failure of any other
call above, is a `SetupError` naming the call and the Windows error number.

## What it does NOT do

- **It does not block the network.** Blocking it for a process needs administrator rights (a firewall rule)
  or an AppContainer. A command can reach the internet, so it can send anything it can read to anyone.
- **It does not limit reads.** A Low token can read every file your account can. So `~/.ssh`, `~/.aws`,
  `~/.gnupg`, browser profiles and sessions, and Ixel's own config folder (which may hold saved keys: `keys.enc`,
  or a plain `.env` on older setups) are readable by the command, and what it prints goes into the run and to
  the model. **Linux hides these folders** (`secret_folders`, bound over with `bwrap`); **Windows can't.** The
  keys saved in Ixel are still kept out of the command's *environment* (`child_env`), and that is all the
  protection there is for them on Windows.
- It does not stop writes to places Windows already labels Low (for example `%USERPROFILE%\AppData\LocalLow`),
  or to any other folder you have labelled Low.
- It does not cover MCP add-ons. `sandboxed_argv` and `can_sandbox` stay False on Windows, so an add-on's
  server runs as before and its approval card says it isn't sandboxed.
- It does not touch Computer access: with it on, commands run without the sandbox and with the network, as
  the warning you confirmed says.

## When it can't be set up

- **One end-to-end check decides the card.** The first time it is asked about (the first card, in a worker
  thread), `winsandbox` runs a tiny `cmd.exe` script through `start()` in a throw-away folder under `%TEMP%`
  and checks that its output comes back, that it can change a file that was there and make a new one, that it
  can write in its temp folder, that it **cannot** write in a sibling folder, and that the folder's label is
  as it was afterwards. It passes or it doesn't, once per Ixel start (it waits up to 20 seconds for the
  test command). Passing makes
  `sandbox_kind()` return `"writes-only"`; failing, or not being on Windows, returns `"none"`.
- **The card says which.** `"writes-only"` shows the amber **Partly sandboxed (Windows)** card (network on,
  reads not limited, in those words). `"none"` shows **Not sandboxed**, and the reason is in
  `sandbox_unavailable_reason()` ("Commands run without a sandbox because …").
- **A setup failure during a run doesn't run the command.** If a call above fails after the card promised
  the sandbox (a label that can't be set, no privilege to start a process, a folder on a drive that holds no
  labels), `run_command` undoes what it did, starts nothing, returns exit code 126 with
  `[not run: the Windows sandbox couldn't be set up: …]` (`CommandResult.started` is False, and the step's
  result says "not run") and **marks the sandbox broken** until Ixel is restarted. The next card then says
  **Not sandboxed**, and the person decides again. It never runs the command unsandboxed after showing the
  sandbox card, and never claims a sandbox it didn't have.
- **A program that isn't there** (`FileNotFoundError`, `PermissionError`) is the command's own failure, as on
  the other systems; it doesn't mark the sandbox broken.
- **The result says what it was.** `CommandResult.sandbox` is `"full"` (macOS, Linux), `"writes-only"` or
  `"none"`; `sandboxed` is True only for `"full"`. The step's result says "sandboxed for writes only
  (Windows): … the network was on and reads weren't limited …".

## Risks

- **It labels the whole workspace, twice per run** (`SetNamedSecurityInfoW` walks every file and folder below
  it to set the label and again to take it off). That can take seconds on a large tree (`node_modules`, a
  big repo). A workspace that is a huge folder of other things is a bad idea; a drive root is refused.
- **A crash leaves the workspace labelled Low.** Quitting Ixel normally (an `atexit` hook) and the end of
  each run put the label back; a hard kill or a power cut during a run can't. The next run then sees a folder
  that is already Low and **leaves it Low**. To clear it by hand: `icacls "<folder>" /setintegritylevel
  "(OI)(CI)M"` (to be checked on Windows). A Low-labelled workspace is not unsafe for Ixel: it only lets
  Low processes write there.
- **Files that were given their own integrity label** inside the workspace (rare; nothing in normal use
  does it) may not come back exactly as they were. This hasn't been checked.
- **Tools that write into your profile fail.** Writes to `%APPDATA%`, `%LOCALAPPDATA%`, `~/.cache`, the
  registry's `HKCU`, a `pip` or `npm` cache, `~/.gitconfig` and the like are refused (Medium). Most
  tools carry on or print a warning; some don't. Output says "Access is denied" or `PermissionError`.
  They can use `%TEMP%`, which is the private folder.
- **Background programs** it leaves running are ended when the job closes (the 2-second drain in
  `run_command`, then `TerminateJobObject`). Anything that relied on a daemon outliving the command breaks.
- **Nested jobs need Windows 8 or newer.** If Ixel itself is in a job (a terminal, a CI runner),
  `AssignProcessToJobObject` fails on Windows 7. Windows 10 and 11 are fine.
- **FAT, exFAT and many network drives hold no labels.** Setting the workspace's label fails there, so a
  run on such a drive returns `[not run: …]` and marks the sandbox broken (above).
- **`CreateProcessAsUserW` with a lowered copy of your own token** is how Microsoft's low-integrity
  guidance starts such a process, and should need no extra privilege for an ordinary user (the tests below
  confirm it). If some policy refuses it (error 1314, a required privilege isn't held), it shows up as a
  setup failure and the card falls back to **Not sandboxed**.
- **Reading other programs.** The no-write-up rule stops a Low process writing into your other programs,
  and the Medium ones' windows. Whether it can *read* another program's memory (Ixel's own, which holds the
  saved keys while it runs) isn't something this design relies on being blocked. The test plan checks it, and the
  answer goes into `SECURITY.md`.
- **Antivirus** products that watch for processes started with a different token may ask or block. A block
  is a setup failure, and the card falls back.
- **A very long command line** (over 32,766 characters) is refused (`OSError`, E2BIG) before anything starts.

## A possible later step: AppContainer

An **AppContainer** process can be denied the network and, because it can open only what grants it
access, most reads too, without administrator rights. It would be the Windows answer to
`bwrap --unshare-net` and the hidden secret folders. The price: the AppContainer's SID has to be granted
access (an ACL entry) on the workspace and on every program folder the command needs (Python, Node, git,
their libraries and caches, the system folders it reads), and those grants have to be undone after, with the
same slow tree walk. It would make `sandboxed` mean the same on all three systems. That is a separate
project; this one is writes-only on purpose and says so.

## Test plan, on a real Windows 10 or 11 machine

1. **Automated first.** `python -m pytest tests/test_agent_winsandbox.py tests/test_agent_sandbox.py
   tests/test_agent_runner.py tests/test_agent_computer.py tests/test_agent_security.py -q`. The Windows-only
   tests in `test_agent_winsandbox.py` run the real thing; one asserts `winsandbox.available()` is True.
2. **Writes in.** In an Agent run on a test folder under `Documents`, approve
   `python -c "open('in.txt','w').write('x')"` (an interpreter with `-c` always asks). `in.txt` exists, and
   modifying an existing file there works too.
3. **Writes out.** Approve a command that writes `C:\Users\<you>\Documents\out.txt` (outside the folder), and
   one that writes `%USERPROFILE%\out.txt`. Both fail with "Access is denied", and neither file exists.
4. **TEMP.** Approve `python -c "import tempfile; print(tempfile.gettempdir())"`: it prints
   `…\ixel-agent-…`, a write there works, and the folder is gone when the run ends.
5. **Stop.** Approve `python -c "import time; time.sleep(300)"` and press Stop: the step ends within a
   second, and `tasklist` shows no `python.exe` left. Repeat with a command that starts a child
   (`python -c "import subprocess,sys,time; subprocess.Popen([sys.executable,'-c','import time; time.sleep(300)']); time.sleep(300)"`):
   both are gone.
6. **The time limit.** Let the same sleeping command run into the 120-second limit (or run
   `run_command(..., timeout=1)` from Python): `timed out`, nothing left running.
7. **64 KB.** Approve `python -c "print('x' * 70000)"`: the result ends with
   `[output cut at 64 KB; it was … bytes in all]`.
8. **The label comes off.** `icacls "<workspace>"` before the run, during it (a sleeping command: it shows a
   `Mandatory Label\Low Mandatory Level` line with `(OI)(CI)(NW)`), and after it: the same as before. Do it again with
   two runs in one folder at once (the label stays until the second ends), and with a folder that was
   already Low (it stays Low).
9. **The network is still on.** Approve `python -c "import urllib.request as u; print(u.urlopen('https://example.com').status)"`:
   it prints 200 **and** the card said "it has the network".
10. **The secret folders are readable.** Approve `python -c "import os; print(os.listdir(os.path.expanduser('~/.ssh')))"`
    on a machine with a test `.ssh` folder: it lists the files, and the card said reads aren't limited.
    Also try to read another running program's memory from the command (`OpenProcess` with
    `PROCESS_VM_READ` on Ixel's own process) and record the answer.
11. **Computer access unchanged.** With it on, the card says **Not sandboxed**, the command can write in
    `Documents`, and it runs the way it did before this change.
12. **Falling back.** Put the workspace on a FAT/exFAT USB stick (or a network share): the run returns
    `[not run: the Windows sandbox couldn't be set up: …]`, and the next card says **Not sandboxed**.
13. **A crash.** Kill Ixel (Task Manager) during a sleeping command: the command ends with it, the workspace
    stays Low (`icacls`), and the manual `icacls … /setintegritylevel "(OI)(CI)M"` clears it.
14. **A downloaded repo.** A command with `cwd` `scratch:NAME`: the card names Ixel's copy, the command can write
    only there and in its temp folder.
15. **Ordinary tools.** `git status`, `python -m pytest -q`, `node --version`, `npm test` in a test project:
    note what fails because it wants to write into the profile, and add it to the list of risks.
