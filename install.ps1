# Installs Ixel MAT into its own virtualenv and puts an `ixel` command on PATH.
#   From a checkout:  powershell -ExecutionPolicy Bypass -File .\install.ps1
# -WaitForPid / -PauseAtEnd are for `ixel update`, which runs this in a new window: Windows
# can't replace the environment's python.exe until the ixel that started the update has exited.
param([int]$WaitForPid = 0, [switch]$PauseAtEnd)
$ErrorActionPreference = 'Stop'
$InstallLock = $null
trap {
    if ($InstallLock) { $InstallLock.Dispose() }
    if ($PauseAtEnd) { Write-Host "Update failed: $_"; Read-Host 'Press Enter to close this window' | Out-Null }
    break
}

$RepoUrl = if ($env:IXEL_REPO_URL) { $env:IXEL_REPO_URL } else { 'https://github.com/OpenIxelAI/ixel-mat.git' }
$Branch = if ($env:IXEL_BRANCH) { $env:IXEL_BRANCH } else { 'main' }
$InstallRoot = if ($env:IXEL_INSTALL_ROOT) { $env:IXEL_INSTALL_ROOT } else { Join-Path $env:LOCALAPPDATA 'IxelMAT' }
$BinDir = if ($env:IXEL_BIN_DIR) { $env:IXEL_BIN_DIR } else { Join-Path $HOME '.local\bin' }
$RepoDir = Join-Path $InstallRoot 'repo'
$VenvDir = Join-Path $InstallRoot '.venv'
$VenvPython = Join-Path $VenvDir 'Scripts\python.exe'
$VenvPythonW = Join-Path $VenvDir 'Scripts\pythonw.exe'
$IxelExe = Join-Path $VenvDir 'Scripts\ixel.exe'
$CmdWrapper = Join-Path $BinDir 'ixel.cmd'

# Held open, unshared, until the install is done (Windows lets it go if the window is closed), so a
# second `ixel update` meanwhile says so instead of starting another install into the same .venv
New-Item -ItemType Directory -Force -Path $InstallRoot | Out-Null
try {
    $InstallLock = [IO.File]::Open((Join-Path $InstallRoot 'install.lock'), 'OpenOrCreate', 'ReadWrite', 'None')
} catch {
    throw 'Ixel is already being installed or updated, in another window. Let that finish, then try again.'
}

# Every Ixel running from this install: the environment's python.exe, which ixel.cmd and the plugin
# start (a launcher, alive while the Python it started runs), its pythonw.exe, which the Start Menu's
# Ixel window starts, and ixel.exe, which an older ixel.cmd started. Windows can't replace them while they run.
function Get-IxelProcesses {
    return @(Get-Process -ErrorAction SilentlyContinue | Where-Object {
        $_.Path -eq $VenvPython -or $_.Path -eq $VenvPythonW -or $_.Path -eq $IxelExe })
}

if ($WaitForPid) {
    Wait-Process -Id $WaitForPid -Timeout 60 -ErrorAction SilentlyContinue
    # The launcher that started the update exits a moment after Python does, and others may be
    # open (a window, a plugin in an app).
    $running = @(Get-IxelProcesses)
    $running | Wait-Process -Timeout 5 -ErrorAction SilentlyContinue
    $running = @(Get-IxelProcesses)
    if ($running.Count) {
        Write-Host 'Waiting for Ixel to close everywhere: another ixel window, or the Ixel plugin in an app such as Claude Desktop...'
        $running | Wait-Process -Timeout 120 -ErrorAction SilentlyContinue
        if (@(Get-IxelProcesses).Count) {
            throw 'Ixel is still running. Close it (and any app using the Ixel plugin), then run ixel update again.'
        }
    }
}

function Require-Command([string]$Name, [string]$Hint) {
    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
        throw "Missing required command: $Name. $Hint"
    }
}

# PowerShell doesn't stop on a failing native command, so check every exit code. Exit
# codes are the signal: a warning pip or git writes to stderr isn't a failure, though
# Windows PowerShell 5.1 can turn one into a terminating error under 'Stop'.
function Invoke-Checked([string]$What, [scriptblock]$Command) {
    $ErrorActionPreference = 'Continue'
    $global:LASTEXITCODE = -1  # stays -1 if the program can't be started at all (Continue only prints that)
    & $Command
    if ($LASTEXITCODE -ne 0) { throw "$What failed (exit code $LASTEXITCODE)" }
}

# The commit a checkout is at: '' if it isn't a git checkout, or git isn't installed
function Get-Commit([string]$Dir) {
    if (-not (Get-Command git -ErrorAction SilentlyContinue)) { return '' }
    $ErrorActionPreference = 'Continue'  # git's stderr isn't a failure (see Invoke-Checked)
    try {
        $commit = & git -C $Dir rev-parse --verify -q HEAD 2>$null
        if ($LASTEXITCODE -eq 0 -and "$commit".Trim() -match '^[0-9a-f]{40,64}$') { return "$commit".Trim() }
    } catch { }
    return ''
}

# A command is an array: the program, then its arguments (e.g. 'py', '-3.13').
# Always splat an array: splatting a lone string passes nothing sensible.
function Split-Command([string[]]$Command) {
    return $Command[0], @($Command | Select-Object -Skip 1)
}

function Test-Python([string[]]$Command) {
    $exe, $rest = Split-Command $Command
    try {
        # 3.10+ and a final release: libraries Ixel needs can break on alphas and release candidates.
        # No double quotes in arguments: Windows PowerShell 5.1 drops them on the way to the program.
        & $exe @rest -c 'import sys; v = sys.version_info; sys.exit(0 if v >= (3, 10) and v.releaselevel == ''final'' else 1)' 2>$null | Out-Null
        return $LASTEXITCODE -eq 0
    } catch {
        return $false
    }
}

function Get-PythonVersion([string[]]$Command) {
    $exe, $rest = Split-Command $Command
    try {
        $version = & $exe @rest -c 'import sys; print(sys.version.split()[0])' 2>$null
        if ($LASTEXITCODE -eq 0 -and $version) { return "$version".Trim() }
    } catch { }
    return $null
}

function Get-PythonCandidates {
    # The py launcher's default first, then each version it knows (newest first), then
    # whatever is on PATH, then python.org's install folders: "Add to PATH" is off by
    # default in its installer, so a working Python is often on disk but not on PATH.
    $candidates = @(@('py', '-3'), @('py', '-3.14'), @('py', '-3.13'), @('py', '-3.12'), @('py', '-3.11'),
                    @('py', '-3.10'), @('python'), @('python3'))
    foreach ($dir in @("$env:LOCALAPPDATA\Programs\Python", $env:ProgramFiles, ${env:ProgramFiles(x86)})) {
        if (-not $dir -or -not (Test-Path $dir)) { continue }
        $installs = Get-ChildItem -Path $dir -Directory -Filter 'Python3*' -ErrorAction SilentlyContinue |
            Sort-Object { [int]('0' + ($_.Name -replace '\D', '')) } -Descending
        foreach ($install in $installs) {
            $exe = Join-Path $install.FullName 'python.exe'
            if (Test-Path $exe) { $candidates += ,@($exe) }
        }
    }
    return $candidates
}

# What each command turned out to be, for the message when none will do.
$script:PythonNotes = @()

function Find-Python {
    $script:PythonNotes = @()
    foreach ($candidate in Get-PythonCandidates) {
        $found = Get-Command $candidate[0] -ErrorAction SilentlyContinue
        if (-not $found) { continue }
        if (Test-Python $candidate) { return ,$candidate }
        if ($candidate[0] -eq 'py' -and $candidate[1] -ne '-3') { continue }  # one line for the launcher
        $name = $candidate -join ' '
        $version = Get-PythonVersion $candidate
        if ($version) {
            $script:PythonNotes += "$name is Python $version (too old, or a pre-release)"
        } elseif ("$($found.Source)" -like '*\WindowsApps\*') {
            $script:PythonNotes += "$name is only the Microsoft Store shortcut, not an installed Python"
        } else {
            $script:PythonNotes += "$name didn't run"
        }
    }
    return $null
}

function Update-SessionPath {
    # Pick up the PATH an installer just changed, without opening a new window
    if ($env:OS -ne 'Windows_NT') { return }
    $machine = [Environment]::GetEnvironmentVariable('Path', 'Machine')
    $user = [Environment]::GetEnvironmentVariable('Path', 'User')
    $env:Path = (@($machine, $user, $env:Path) | Where-Object { $_ }) -join ';'
}

function Get-PythonCommand {
    if ($env:IXEL_PYTHON) {
        if (Test-Python @($env:IXEL_PYTHON)) { return ,@($env:IXEL_PYTHON) }
        throw "IXEL_PYTHON=$($env:IXEL_PYTHON) isn't a final release of Python 3.10 or newer."
    }
    $python = Find-Python
    if ($python) { return ,$python }

    Write-Host "Ixel MAT needs Python 3.10 or newer, and this computer doesn't have one yet."
    foreach ($note in $script:PythonNotes) { Write-Host "  - $note" }
    $canAsk = [Environment]::UserInteractive -and -not $env:CI
    if ($canAsk -and (Get-Command winget -ErrorAction SilentlyContinue)) {
        $answer = Read-Host "Install Python 3.13 now with winget (Microsoft's installer)? [Y/n]"
        if ($answer -notmatch '^\s*[Nn]') {
            # Its exit code isn't a verdict ("already installed" is non-zero), so just look again.
            # Out-Host: anything a program prints inside a function becomes its return value.
            & winget install --id Python.Python.3.13 --exact --source winget --accept-package-agreements --accept-source-agreements |
                Out-Host
            Update-SessionPath
            $python = Find-Python
            if ($python) { return ,$python }
            Write-Host "Python was installed, but this window can't see it yet."
        }
    }
    throw ("Install Python with:  winget install Python.Python.3.13   (or from https://www.python.org/downloads/)," +
           " then open a new PowerShell window and run this again.")
}

function Ensure-UserPath([string]$Dir) {
    if ($env:IXEL_SKIP_PATH_UPDATE -eq '1') { return }
    $userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
    $parts = @()
    if ($userPath) { $parts = $userPath.Split(';') | Where-Object { $_ } }
    if ($parts -contains $Dir) { return }
    $newPath = if ($userPath) { "$userPath;$Dir" } else { $Dir }
    [Environment]::SetEnvironmentVariable('Path', $newPath, 'User')
}

$Python = Get-PythonCommand
New-Item -ItemType Directory -Force -Path $InstallRoot | Out-Null
New-Item -ItemType Directory -Force -Path $BinDir | Out-Null

# Install the checkout this script lives in, if it is one; otherwise clone.
$ScriptDir = if ($PSScriptRoot) { $PSScriptRoot } else { '' }
if ($ScriptDir -and (Test-Path (Join-Path $ScriptDir 'pyproject.toml')) -and (Test-Path (Join-Path $ScriptDir 'ixel_mat'))) {
    $SourceDir = $ScriptDir
} else {
    Require-Command git 'Install it with:  winget install Git.Git'
    if (Test-Path (Join-Path $RepoDir '.git')) {
        Invoke-Checked 'git fetch' { git -C $RepoDir fetch origin }
        Invoke-Checked 'git checkout' { git -C $RepoDir checkout $Branch }
        Invoke-Checked 'git pull' { git -C $RepoDir pull --ff-only origin $Branch }
    } else {
        if (Test-Path $RepoDir) { Remove-Item -Recurse -Force $RepoDir }
        Invoke-Checked 'git clone' { git clone --branch $Branch $RepoUrl $RepoDir }
    }
    $SourceDir = $RepoDir
}

$PyExe, $PyArgs = Split-Command $Python
Write-Host "Using $(& $PyExe @PyArgs --version 2>&1)"
if ($env:IXEL_USE_UV -ne '0' -and (Get-Command uv -ErrorAction SilentlyContinue)) {
    $BasePython = & $PyExe @PyArgs -c 'import sys; print(sys.executable)'
    Invoke-Checked 'uv venv' { uv venv --quiet --allow-existing --python $BasePython $VenvDir }
    Invoke-Checked 'uv pip install' { uv pip install --quiet --python $VenvPython --upgrade $SourceDir }
} else {
    Invoke-Checked 'Creating the virtual environment' { & $PyExe @PyArgs -m venv $VenvDir }
    Invoke-Checked 'pip upgrade' { & $VenvPython -m pip install --quiet --upgrade pip }
    Invoke-Checked 'pip install' { & $VenvPython -m pip install --quiet --upgrade $SourceDir }
}
# Import everything a review uses, not just the entry point: a broken dependency
# should fail the install, not the first question. -I, as ixel.cmd runs it (below).
Invoke-Checked 'Loading Ixel MAT' { & $VenvPython -I -c 'import ixel_mat.agents.http, ixel_mat.mcp_server, ixel_mat.gui.server' }

# ixel.cmd runs the environment's python.exe (signed), not the ixel.exe pip writes (unsigned, so Smart
# App Control can block it). -I: nothing is imported from the folder you run ixel in (python -m would
# look there first, and it may be a repository you just cloned), and PYTHON* variables are ignored.
# It's ASCII (cmd.exe reads it in the console's code page), which writes an accented letter in a user
# folder's name as ?: under %LOCALAPPDATA%, as by default, the path names that variable, and cmd fills it in.
$WrapperPython = $VenvPython
if ($env:LOCALAPPDATA -and $VenvPython.StartsWith($env:LOCALAPPDATA + '\', [StringComparison]::OrdinalIgnoreCase)) {
    $WrapperPython = '%LOCALAPPDATA%' + $VenvPython.Substring($env:LOCALAPPDATA.Length)
}
Set-Content -Path $CmdWrapper -Encoding ASCII -Value "@echo off`r`n`"$WrapperPython`" -I -m ixel_mat %*"

Ensure-UserPath $BinDir

# Start Menu entry "Ixel": `ixel app` (Ixel in a window of its own) through the environment's pythonw.exe,
# which is signed (Smart App Control lets it run) and leaves no console window behind the app.
# `ixel update` makes it again (so it follows the install); IXEL_SKIP_APP_ENTRY=1 (scripts/check_windows.py's
# trial installs) leaves the Start Menu alone. It never replaces a shortcut that isn't Ixel's: beside an
# Ixel.lnk of your own, it's "Ixel MAT".
$Programs = [Environment]::GetFolderPath('Programs')
$WShell = try { New-Object -ComObject WScript.Shell } catch { $null }
function Test-IxelShortcut([string]$Path) {
    # A shortcut that won't load isn't Ixel's (and mustn't stop the install)
    try {
        [bool]($WShell -and $Path -and (Test-Path -LiteralPath $Path) -and
               $WShell.CreateShortcut($Path).Arguments -eq '-I -m ixel_mat app')
    } catch { $false }
}
$Shortcut = Join-Path $Programs 'Ixel.lnk'
$Beside = Join-Path $Programs 'Ixel MAT.lnk'
if ((Test-Path -LiteralPath $Shortcut) -and -not (Test-IxelShortcut $Shortcut)) {
    $Shortcut = if ((Test-Path -LiteralPath $Beside) -and -not (Test-IxelShortcut $Beside)) { $null } else { $Beside }
}
if ($env:IXEL_SKIP_APP_ENTRY -ne '1' -and $WShell -and (Test-Path $VenvPythonW)) {
    if ($Shortcut) {
        $Icon = Join-Path $VenvDir 'Lib\site-packages\ixel_mat\assets\ixel.ico'
        $Lnk = $WShell.CreateShortcut($Shortcut)
        $Lnk.TargetPath = $VenvPythonW
        $Lnk.Arguments = '-I -m ixel_mat app'
        $Lnk.WorkingDirectory = $InstallRoot
        $Lnk.Description = 'Ixel: ask your AI models as a panel'
        if (Test-Path $Icon) { $Lnk.IconLocation = "$Icon,0" }
        $Lnk.Save()
        # Back under its own name: the Ixel MAT shortcut an earlier install made beside another Ixel goes
        if ($Shortcut -ne $Beside -and (Test-IxelShortcut $Beside)) { Remove-Item -LiteralPath $Beside -Force }
    } else {
        Write-Warning "The Start Menu already has Ixel and Ixel MAT shortcuts that aren't Ixel's, so it's left as it is (ixel app still works)"
    }
}

# Last, so it's only written once everything above worked: where this install came from and the
# commit it installed, for `ixel update` (which installs again if the checkout has moved on since).
# No double quotes: see Test-Python. An empty commit may not reach Python at all (5.1 drops ''), so
# it's read as an optional last argument.
$Commit = Get-Commit $SourceDir
Invoke-Checked 'Recording the install' {
    & $VenvPython -c 'import json, sys; info = dict(source=sys.argv[2], install_root=sys.argv[3], bin_dir=sys.argv[4], installer=''install.ps1''); info.update(zip([''commit''], filter(None, sys.argv[5:]))); json.dump(info, open(sys.argv[1], ''w'', encoding=''utf-8''))' (Join-Path $InstallRoot 'install.json') $SourceDir $InstallRoot $BinDir $Commit
}

Write-Host ""
Write-Host "Ixel MAT installed from $SourceDir"
Write-Host "Command: $CmdWrapper"
$OurShortcut = Test-IxelShortcut $Shortcut
if ($OurShortcut) {
    $Name = [IO.Path]::GetFileNameWithoutExtension($Shortcut)
    Write-Host "App:     $Name in the Start Menu (or: ixel app)"
    if ($Name -ne 'Ixel') { Write-Host "  Named $Name because you have an Ixel shortcut of your own, which is left as it is." }
}
Write-Host "Next:    ixel setup"
Write-Host "Update:  ixel update"
$Remove = "'$InstallRoot', '$CmdWrapper'" + $(if ($OurShortcut) { ", '$Shortcut'" } else { '' })
Write-Host "Remove:  Remove-Item -Recurse -Force $Remove"
Write-Host "If the command is not found in this shell yet, restart PowerShell or run:"
Write-Host "  `$env:Path = '$BinDir;' + `$env:Path"
$InstallLock.Dispose()
if ($PauseAtEnd) { Read-Host 'Press Enter to close this window' | Out-Null }
