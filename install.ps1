# install.ps1: the installer for vcharon on Windows.
# Usage (PowerShell):
#   irm https://raw.githubusercontent.com/zhoufanscut/VCharon/main/install.ps1 | iex
#
# Checks for 64-bit Windows on x64, downloads vcharon-win-x64.zip of the latest GitHub release
# and its .sha256, checks the hash (a mismatch fails), takes only vcharon.exe out of the zip,
# tries it, and puts it at %LOCALAPPDATA%\Programs\vcharon\vcharon.exe; then adds that folder
# to your user PATH if it is missing. The same steps as vcharon --update (DESIGN, "Self-update").
# Linux and macOS have install.sh.
#
# All of it runs in one script block, so nothing stays defined in your session. Run through
# iex, it never calls exit (that would close your PowerShell window); run as a file
# (powershell -File install.ps1) it exits 1 on a failure.

& {
    $ErrorActionPreference = 'Stop'
    # Invoke-WebRequest's progress bar makes Windows PowerShell 5.1's downloads very slow
    $ProgressPreference = 'SilentlyContinue'

    $Repo = 'zhoufanscut/VCharon'
    $Asset = 'vcharon-win-x64.zip'
    $ExeName = 'vcharon.exe'
    # the most the binary in the zip may hold; a bigger one is refused, never written
    $MaxBinary = 200000000
    # the new binary's --version: a one-file binary unpacks itself first
    $VersionTimeoutMs = 60000
    $PipxLine = "  pipx install git+https://github.com/$Repo"

    # The HTTP status of a failed web request, or 0 when no answer came (DNS, TLS, a refused
    # connection). PowerShell 5.1 throws a WebException and 7 an HttpResponseException; both
    # carry a Response whose StatusCode is an HttpStatusCode.
    function Get-HttpStatus($failure) {
        $response = $failure.Exception.Response
        if ($null -eq $response) { return 0 }
        try { return [int]$response.StatusCode } catch { return 0 }
    }

    # Move-Item, tried 5 times 0.5 s apart: antivirus often holds a new file for a moment.
    function Move-Retrying($from, $to) {
        for ($try = 1; ; $try++) {
            try {
                Move-Item -LiteralPath $from -Destination $to
                return
            } catch {
                if ($try -ge 5) { throw }
                Start-Sleep -Milliseconds 500
            }
        }
    }

    # Whether a URL override is one the tests use: https anywhere, http only to this machine
    # (host exactly 127.0.0.1 or localhost, any port). Never one with a user part (an @):
    # what comes before it would look like the host.
    function Test-InstallUrl($text) {
        if ($text.Contains('@')) { return $false }
        $uri = $null
        if (-not [Uri]::TryCreate($text, [UriKind]::Absolute, [ref]$uri)) { return $false }
        if ($uri.UserInfo) { return $false }
        if ($uri.Scheme -eq 'https') { return $true }
        return ($uri.Scheme -eq 'http' -and
                ($uri.Host -eq '127.0.0.1' -or $uri.Host -eq 'localhost'))
    }

    function Install-VCharon {
        # Where the release is read from. Only the installer's own tests set these, to a
        # local fake release; nothing else should.
        $ApiUrl = $env:VCHARON_INSTALL_API_URL
        if (-not $ApiUrl) { $ApiUrl = "https://api.github.com/repos/$Repo/releases/latest" }
        $DownloadRoot = $env:VCHARON_INSTALL_DOWNLOAD_URL
        if (-not $DownloadRoot) { $DownloadRoot = "https://github.com/$Repo/releases/download" }
        if ($env:VCHARON_INSTALL_API_URL -or $env:VCHARON_INSTALL_DOWNLOAD_URL) {
            foreach ($one in @($ApiUrl, $DownloadRoot)) {
                if (-not (Test-InstallUrl $one)) {
                    throw "an install URL must be https, or http to this machine: $one"
                }
            }
        }

        # --- this machine ---
        # a 32-bit PowerShell on 64-bit Windows says x86 here and AMD64 in
        # PROCESSOR_ARCHITEW6432
        $arch = $env:PROCESSOR_ARCHITEW6432
        if (-not $arch) { $arch = $env:PROCESSOR_ARCHITECTURE }
        if ($arch -ne 'AMD64') {
            throw ("no binary is published for Windows on $arch (only x64); install with " +
                   "pipx:`n$PipxLine")
        }

        # Windows PowerShell 5.1 may not offer TLS 1.2 by default; GitHub needs it
        if ($PSVersionTable.PSVersion.Major -lt 6) {
            [Net.ServicePointManager]::SecurityProtocol = `
                [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
        }

        # --- the latest release ---
        Write-Host "Fetching the latest release from $ApiUrl ..."
        $release = Invoke-RestMethod -Uri $ApiUrl -UseBasicParsing `
            -Headers @{ 'User-Agent' = 'vcharon-install' }
        $tag = [string]$release.tag_name
        if (-not $tag) { throw 'could not read the latest release''s tag' }
        $version = $tag -replace '^[vV]', ''
        $url = "$DownloadRoot/$tag/$Asset"

        $dir = Join-Path $env:LOCALAPPDATA 'Programs\vcharon'
        $dest = Join-Path $dir $ExeName
        New-Item -ItemType Directory -Force -Path $dir | Out-Null
        # the work folder sits in the install folder: one volume, so the last move is a rename
        $work = Join-Path $dir ('.vcharon-install-' + [guid]::NewGuid().ToString('N'))
        New-Item -ItemType Directory -Path $work | Out-Null
        # the running binary's copy, once it has been renamed away
        $old = $null
        try {
            # --- download and check ---
            $zip = Join-Path $work $Asset
            Write-Host "Downloading $Asset ($tag) ..."
            Invoke-WebRequest -Uri $url -OutFile $zip -UseBasicParsing

            # Only a release without a checksum file (404) goes on unchecked; any other
            # failure to get it is an error, never "not checked".
            $sums = Join-Path $work "$Asset.sha256"
            $haveSums = $true
            try {
                Invoke-WebRequest -Uri "$url.sha256" -OutFile $sums -UseBasicParsing
            } catch {
                $status = Get-HttpStatus $_
                if ($status -ne 404) {
                    $why = if ($status) { "HTTP $status" } else { $_.Exception.Message }
                    throw "couldn't download $Asset.sha256 ($why); nothing was installed"
                }
                $haveSums = $false
            }
            if ($haveSums) {
                Write-Host 'Checking the checksum ...'
                # one line, "<hex>  <file name>": the first word counts, as for --update
                $line = Get-Content -LiteralPath $sums -TotalCount 1
                $expected = (([string]$line).Trim() -split '\s+')[0].ToLowerInvariant()
                if ($expected -notmatch '^[0-9a-f]{64}$') { throw "$Asset.sha256 holds no sha256" }
                $actual = (Get-FileHash -LiteralPath $zip -Algorithm SHA256).Hash.ToLowerInvariant()
                if ($actual -ne $expected) {
                    throw ("checksum mismatch for ${Asset}: expected $expected, got $actual; " +
                           'nothing was installed')
                }
            } else {
                Write-Warning 'this release has no checksum file; the checksum is not checked'
            }

            # --- only vcharon.exe, never the whole zip ---
            Write-Host 'Extracting ...'
            Add-Type -AssemblyName System.IO.Compression
            Add-Type -AssemblyName System.IO.Compression.FileSystem
            $staged = Join-Path $work 'vcharon.new.exe'
            $archive = [System.IO.Compression.ZipFile]::OpenRead($zip)
            try {
                # the entry named exactly vcharon.exe: no folder part, so not a folder either
                $entry = $null
                foreach ($one in $archive.Entries) {
                    if ($one.FullName -ceq $ExeName) { $entry = $one; break }
                }
                if ($null -eq $entry) { throw "$Asset holds no $ExeName" }
                if ($entry.Length -gt $MaxBinary) {
                    throw ("$ExeName in $Asset is $($entry.Length) bytes, over the " +
                           "$MaxBinary allowed")
                }
                $in = $entry.Open()
                try {
                    $out = [System.IO.File]::Create($staged)
                    try {
                        $buffer = New-Object byte[] 1048576
                        $total = 0
                        while (($n = $in.Read($buffer, 0, $buffer.Length)) -gt 0) {
                            $total += $n
                            if ($total -gt $MaxBinary) {
                                throw "$ExeName in $Asset is over the $MaxBinary bytes allowed"
                            }
                            $out.Write($buffer, 0, $n)
                        }
                    } finally { $out.Dispose() }
                } finally { $in.Dispose() }
            } finally { $archive.Dispose() }

            # it must run here, within the time limit, and be the release's version, before
            # anything is replaced
            $got = ''
            $versionOut = Join-Path $work 'version.out'
            try {
                $proc = Start-Process -FilePath $staged -ArgumentList '--version' -NoNewWindow `
                    -PassThru -RedirectStandardOutput $versionOut `
                    -RedirectStandardError (Join-Path $work 'version.err')
                if ($proc.WaitForExit($VersionTimeoutMs)) {
                    $got = ([string](Get-Content -LiteralPath $versionOut -Raw)).Trim()
                } else {
                    # best effort: a one-file binary's own child first (found by its parent
                    # id, while that is still this process), then the binary
                    try {
                        Get-CimInstance Win32_Process -Filter "ParentProcessId = $($proc.Id)" |
                            ForEach-Object {
                                Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
                            }
                    } catch { }
                    try { $proc.Kill() } catch { }
                    $got = "(no answer in $($VersionTimeoutMs / 1000) s)"
                }
            } catch { $got = '' }
            if ($got -ne $version) {
                throw ("the downloaded binary didn't run here (its --version printed '$got', " +
                       "expected '$version'); nothing was installed. Install with pipx " +
                       "instead:`n$PipxLine")
            }

            # --- install ---
            # A running vcharon.exe can't be replaced or deleted, but it can be renamed: to a
            # unique vcharon.exe.old-<unix time>, deleted by a later start of vcharon.
            $moved = $false
            try {
                if (Test-Path -LiteralPath $dest) {
                    $t = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
                    while (Test-Path -LiteralPath "$dest.old-$t") { $t++ }
                    $old = "$dest.old-$t"
                    Move-Retrying $dest $old
                }
                Move-Retrying $staged $dest
                $moved = $true
            } finally {
                # any way out, a Ctrl-C too: the old one goes back while nothing is at $dest
                if (-not $moved -and $old -and (Test-Path -LiteralPath $old) -and
                        -not (Test-Path -LiteralPath $dest)) {
                    try {
                        Move-Retrying $old $dest
                    } catch {
                        throw ("the new binary couldn't be moved to $dest, and the old one " +
                               "couldn't be put back: it is $old now; rename it back to $dest")
                    }
                }
            }
            if ($old) {
                # not while it runs: a later start of vcharon deletes it then
                Remove-Item -LiteralPath $old -Force -ErrorAction SilentlyContinue
            }
        } finally {
            # never while the old binary was moved and none is at $dest: the work folder may
            # hold the only good copy left
            if ($old -and -not (Test-Path -LiteralPath $dest)) {
                Write-Warning "no binary is at $dest; the download is kept in $work"
            } else {
                Remove-Item -LiteralPath $work -Recurse -Force -ErrorAction SilentlyContinue
            }
        }
        Write-Host ''
        Write-Host "Installed: $dest ($version)"

        # --- the user's PATH ---
        # read and written raw, so entries such as %USERPROFILE%\bin stay unexpanded
        $key = Get-Item -LiteralPath 'HKCU:\Environment'
        $userPath = [string]$key.GetValue('Path', '', 'DoNotExpandEnvironmentNames')
        $kind = 'ExpandString'
        if ($userPath -and $key.GetValueKind('Path') -eq 'String') { $kind = 'String' }
        $parts = @($userPath -split ';' | Where-Object { $_ -ne '' })
        $there = $false
        foreach ($part in $parts) {
            $expanded = [Environment]::ExpandEnvironmentVariables($part).TrimEnd('\')
            if ($expanded -ieq $dir.TrimEnd('\')) { $there = $true; break }
        }
        if ($there) {
            Write-Host "$dir is on your user PATH already."
        } else {
            $newPath = (@($parts) + $dir) -join ';'
            Set-ItemProperty -LiteralPath 'HKCU:\Environment' -Name 'Path' -Value $newPath `
                -Type $kind
            # tell Explorer, so terminals opened from now on see the new PATH; the PATH is
            # set either way, so a failure here is only a warning
            try {
                if (-not ('VCharonInstall.Env' -as [type])) {
                    Add-Type -Namespace VCharonInstall -Name Env -MemberDefinition @'
[DllImport("user32.dll", SetLastError = true, CharSet = CharSet.Auto)]
public static extern IntPtr SendMessageTimeout(IntPtr hWnd, uint Msg, UIntPtr wParam,
    string lParam, uint fuFlags, uint uTimeout, out UIntPtr lpdwResult);
'@
                }
                $result = [UIntPtr]::Zero
                [VCharonInstall.Env]::SendMessageTimeout([IntPtr]0xffff, 0x1A, [UIntPtr]::Zero,
                    'Environment', 2, 5000, [ref]$result) | Out-Null
            } catch {
                Write-Warning ("couldn't tell Windows the PATH changed: $($_.Exception.Message); " +
                               'sign out and in again if a new terminal doesn''t find vcharon')
            }
            Write-Host "Added $dir to your user PATH; open a new terminal to use it."
        }
        # this session too
        $inSession = ($env:Path -split ';') |
            Where-Object { $_.TrimEnd('\') -ieq $dir.TrimEnd('\') }
        if (-not $inSession) { $env:Path = "$env:Path;$dir" }
        Write-Host ''
        Write-Host 'Next:'
        Write-Host '  vcharon --version        check that it runs'
        Write-Host '  vcharon skill install    so Claude Code and Codex find vcharon: a skill that'
        Write-Host '                           points them at vcharon guide'
        Write-Host '  vcharon guide            the agent guide'
    }

    try {
        Install-VCharon
    } catch {
        Write-Host "error: $($_.Exception.Message)" -ForegroundColor Red
        # as a file, a failure is exit 1; through iex, exit would close the window
        if ($PSCommandPath) { exit 1 }
    }
}
