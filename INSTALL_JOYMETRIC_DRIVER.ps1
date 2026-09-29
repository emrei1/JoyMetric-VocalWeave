param(
    [switch]$ForceRebuild,
    [switch]$ForceReinstall,
    [switch]$BootstrapTools,
    [string]$LogPath = ''
)
$ErrorActionPreference = 'Stop'
$ROOT = Split-Path -Parent $MyInvocation.MyCommand.Path
if(-not $LogPath){
    $logDir = Join-Path $env:LOCALAPPDATA 'JoyMetric\logs'
    New-Item -ItemType Directory -Force -Path $logDir | Out-Null
    $LogPath = Join-Path $logDir 'driver-install-v30.4.42.log'
}
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $LogPath) | Out-Null
try { Start-Transcript -Path $LogPath -Force | Out-Null } catch {}

function Stop-Log { try { Stop-Transcript | Out-Null } catch {} }
function Assert-Admin {
    $id=[Security.Principal.WindowsIdentity]::GetCurrent()
    $p=New-Object Security.Principal.WindowsPrincipal($id)
    if(-not $p.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)){
        throw 'Run INSTALL_JOYMETRIC_DRIVER.ps1 as Administrator.'
    }
}
function Find-Tool([string]$Name,[string[]]$Roots){
    foreach($r in $Roots){
        if($r -and (Test-Path $r)){
            $x=Get-ChildItem $r -Filter $Name -File -Recurse -ErrorAction SilentlyContinue | Sort-Object FullName -Descending | Select-Object -First 1
            if($x){ return $x.FullName }
        }
    }
    return $null
}
function Get-Toolchain {
    $result=[ordered]@{VsWhere=$null;VsRoot=$null;MsBuild=$null;Kits=$null;WdkVersion=$null;Inf2Cat=$null;SignTool=$null;InfVerif=$null;ApiValidator=$null;ApiExtractorDir=$null;UniversalDDIs=$null;ModuleWhiteList=$null;ApiValidationAvailable=$false}
    $vswhere = Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio\Installer\vswhere.exe'
    if(Test-Path $vswhere){
        $result.VsWhere=$vswhere
        $vsroot = (& $vswhere -latest -products * -requires Microsoft.Component.MSBuild -property installationPath | Select-Object -First 1)
        if($vsroot){
            $result.VsRoot=$vsroot
            $msbuild=Join-Path $vsroot 'MSBuild\Current\Bin\MSBuild.exe'
            if(Test-Path $msbuild){$result.MsBuild=$msbuild}
        }
    }
    $kits=Join-Path ${env:ProgramFiles(x86)} 'Windows Kits\10'
    if(Test-Path $kits){
        $result.Kits=$kits
        $binRoot=Join-Path $kits 'bin'
        $verDirs = Get-ChildItem $binRoot -Directory -ErrorAction SilentlyContinue |
            Where-Object { $_.Name -match '^10\.0\.\d+\.\d+$' } |
            Sort-Object { [version]$_.Name } -Descending

        # WDK/SDK host tools do NOT have to match the target driver's architecture.
        # Prefer x64 host tools where installed, but accept x86 copies (notably
        # Inf2Cat, which some WDK layouts install only under x86).
        foreach($vd in $verDirs){
            $x64=Join-Path $vd.FullName 'x64'
            $x86=Join-Path $vd.FullName 'x86'
            $infCandidates=@((Join-Path $x64 'Inf2Cat.exe'),(Join-Path $x86 'Inf2Cat.exe'))
            $sigCandidates=@((Join-Path $x64 'signtool.exe'),(Join-Path $x86 'signtool.exe'))
            $inf2=$infCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1
            $sig=$sigCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1
            if($inf2 -or $sig){
                if(-not $result.WdkVersion){$result.WdkVersion=$vd.Name}
                if(-not $result.Inf2Cat -and $inf2){$result.Inf2Cat=$inf2}
                if(-not $result.SignTool -and $sig){$result.SignTool=$sig}
            }
            # ApiValidator must match the x64 target. Only enable it when the
            # complete x64 validation set exists. WDK 28000 installations have
            # been observed with only the x86 host validator, which returns 193
            # against an x64 driver. That is a WDK layout issue, not a JoyMetric
            # compile failure, so development installation must not be blocked.
            if(-not $result.ApiValidator){
                $api=Join-Path $x64 'ApiValidator.exe'
                $ait=Join-Path $x64 'Aitstatic.exe'
                $apiDll=Join-Path $x64 'Microsoft.Kits.Drivers.ApiValidator.dll'
                $uddi=Join-Path $kits ("build\{0}\universalDDIs\x64\UniversalDDIs.xml" -f $vd.Name)
                $white=Join-Path $kits ("build\{0}\universalDDIs\x64\ModuleWhiteList.xml" -f $vd.Name)
                if((Test-Path $api) -and (Test-Path $ait) -and (Test-Path $apiDll) -and (Test-Path $uddi) -and (Test-Path $white)){
                    $result.ApiValidator=$api
                    $result.ApiExtractorDir=$x64
                    $result.UniversalDDIs=$uddi
                    $result.ModuleWhiteList=$white
                    $result.ApiValidationAvailable=$true
                    if(-not $result.WdkVersion){$result.WdkVersion=$vd.Name}
                }
            }
        }
        if(-not $result.Inf2Cat){$result.Inf2Cat=Find-Tool 'Inf2Cat.exe' @($binRoot)}
        if(-not $result.SignTool){$result.SignTool=Find-Tool 'signtool.exe' @($binRoot)}

        # InfVerif is an INF parser/validator host tool. Prefer x64, then x86,
        # then any installed copy. It validates the INF, not executable machine code.
        $ivCandidates=@(
            (Join-Path $kits 'Tools\x64\InfVerif.exe'),
            (Join-Path $kits 'Tools\x86\InfVerif.exe'),
            (Join-Path $kits 'tools\x64\InfVerif.exe'),
            (Join-Path $kits 'tools\x86\InfVerif.exe')
        )
        $result.InfVerif=$ivCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1
        if(-not $result.InfVerif){$result.InfVerif=Find-Tool 'InfVerif.exe' @($kits)}
    }
    return [pscustomobject]$result
}
function Install-DriverToolchain {
    $winget=Get-Command winget.exe -ErrorAction SilentlyContinue
    if(-not $winget){ throw 'WinGet is required to bootstrap Visual Studio/SDK/WDK automatically.' }
    $cfg=Join-Path $env:TEMP 'joymetric-wdk-vscommunity.dsc.yaml'
    $uri='https://raw.githubusercontent.com/microsoft/Windows-driver-samples/main/_wdk_utils/winget/configs/wdk-vscommunity.dsc.yaml'
    Write-Host 'Driver build tools are missing. Installing Microsoft Visual Studio + SDK + WDK using Microsoft official WinGet configuration...' -ForegroundColor Yellow
    Write-Host 'This is a large one-time Microsoft toolchain install.' -ForegroundColor Yellow
    Invoke-WebRequest -UseBasicParsing -Uri $uri -OutFile $cfg
    & $winget.Source configure -f $cfg --accept-configuration-agreements --disable-interactivity | Out-Host
    if($LASTEXITCODE -ne 0){ throw "WinGet WDK configuration failed with exit code $LASTEXITCODE." }
}

try {
    Assert-Admin
    Write-Host ''
    Write-Host 'JoyMetric Virtual Audio Driver v30.4.42 - pristine render-endpoint installer' -ForegroundColor Cyan
    Write-Host "Log: $LogPath" -ForegroundColor DarkGray
    Write-Host ''

    # Development kernel driver signing gate. Never alter Secure Boot automatically.
    $secureBoot=$false
    try { $secureBoot=[bool](Confirm-SecureBootUEFI) } catch { $secureBoot=$false }
    $bc = (& bcdedit /enum '{current}' | Out-String)
    $testSigning = ($bc -match '(?im)^\s*testsigning\s+Yes\s*$')
    Write-Host "Secure Boot: $secureBoot"
    Write-Host "TESTSIGNING: $testSigning"
    if(-not $testSigning){
        if($secureBoot){
            throw @'
SECURE_BOOT_ON
Secure Boot is ON. Windows will not load this locally test-signed JoyMetric kernel driver.
Microsoft requires Secure Boot to be disabled before TESTSIGNING can be enabled for a locally test-signed development driver.
JoyMetric will NOT disable Secure Boot or BitLocker automatically. For a normal-user release, the driver must be Microsoft production-signed.
'@
        }
        Write-Host 'Enabling TESTSIGNING for this development driver...' -ForegroundColor Yellow
        $out = (& bcdedit /set testsigning on 2>&1 | Out-String)
        $out | Out-Host
        if($LASTEXITCODE -ne 0){ throw "bcdedit could not enable TESTSIGNING: $out" }
        Write-Host 'TESTSIGNING was enabled. Windows must reboot once before the driver can load.' -ForegroundColor Yellow
        Stop-Log
        exit 3010
    }

    $tools=Get-Toolchain
    if(-not $tools.MsBuild -or -not $tools.Inf2Cat -or -not $tools.SignTool){
        if(-not $BootstrapTools){
            throw 'Visual Studio C++/Windows SDK/WDK build tools are missing.'
        }
        Install-DriverToolchain
        $tools=Get-Toolchain
    }
    if(-not $tools.MsBuild){throw 'MSBuild still not found after toolchain bootstrap.'}
    if(-not $tools.Inf2Cat -or -not $tools.SignTool){throw 'WDK Inf2Cat/SignTool host tools still not found after toolchain bootstrap.'}
    if(-not $tools.InfVerif){throw 'WDK x64 InfVerif could not be found.'}
        Write-Host "MSBuild: $($tools.MsBuild)" -ForegroundColor DarkGray
    Write-Host "WDK: $($tools.WdkVersion)" -ForegroundColor DarkGray
    Write-Host "Inf2Cat host tool: $($tools.Inf2Cat)" -ForegroundColor DarkGray
    Write-Host "SignTool host tool: $($tools.SignTool)" -ForegroundColor DarkGray
    Write-Host "InfVerif host tool: $($tools.InfVerif)" -ForegroundColor DarkGray
    if($tools.ApiValidationAvailable){ Write-Host "ApiValidator x64: $($tools.ApiValidator)" -ForegroundColor DarkGray } else { Write-Warning "Complete x64 ApiValidator set is not installed in this WDK layout. Development install will continue after InfVerif /w; production submission should run Microsoft validation on a complete WDK environment." }

    $cfg = Get-Content (Join-Path $ROOT 'config.json') -Raw | ConvertFrom-Json
    $conda = [string]$cfg.conda_exe
    $py = $null
    if(Test-Path $conda){
        try {
            $envs = (& $conda env list --json | ConvertFrom-Json).envs
            $ro = $envs | Where-Object { (Split-Path $_ -Leaf) -eq [string]$cfg.roformer_env } | Select-Object -First 1
            if($ro -and (Test-Path (Join-Path $ro 'python.exe'))){ $py=Join-Path $ro 'python.exe' }
        } catch {}
    }
    if(-not $py){
        $cmd=Get-Command python.exe -ErrorAction SilentlyContinue
        if($cmd){$py=$cmd.Source}
    }
    if(-not $py){ throw 'Python could not be found for the JoyMetric driver patch step.' }

    $joyRoot = Join-Path $env:LOCALAPPDATA 'JoyMetric'
    $driverRoot = Join-Path $joyRoot 'virtual-driver'
    $repo = Join-Path $driverRoot 'windows-driver-samples'
    $build = Join-Path $driverRoot 'JoyMetricSimpleAudio'
    New-Item -ItemType Directory -Force -Path $driverRoot | Out-Null

    # v30.4.42: ForceRebuild means *pristine upstream*, not merely a fresh
    # derivative build directory. Earlier JoyMetric development builds reused
    # this sparse checkout, so discard it completely before the render-safe
    # rebuild. This guarantees Microsoft's stock WaveRT speaker miniport is the
    # source of truth.
    if($ForceRebuild -and (Test-Path $repo)){
        Write-Host 'Refreshing pristine Microsoft SimpleAudioSample source...' -ForegroundColor Yellow
        Remove-Item $repo -Recurse -Force
    }

    $needRepo = -not (Test-Path (Join-Path $repo 'audio\simpleaudiosample\SimpleAudioSample.sln')) -or -not (Test-Path (Join-Path $repo 'setup\devcon\devcon.vcxproj'))
    if($needRepo){
        if(Test-Path $repo){Remove-Item $repo -Recurse -Force}
        $git=Get-Command git.exe -ErrorAction SilentlyContinue
        if($git){
            Write-Host 'Downloading Microsoft driver sample framework + DevCon source...' -ForegroundColor Yellow
            & $git.Source clone --depth 1 --filter=blob:none --sparse https://github.com/microsoft/Windows-driver-samples.git $repo
            if($LASTEXITCODE -ne 0){throw 'Git clone of Windows-driver-samples failed.'}
            & $git.Source -C $repo sparse-checkout set audio/simpleaudiosample setup/devcon
            if($LASTEXITCODE -ne 0){throw 'Git sparse checkout failed.'}
        } else {
            Write-Host 'Git not found; downloading Microsoft driver samples ZIP...' -ForegroundColor Yellow
            $zip=Join-Path $env:TEMP 'joymetric_windows_driver_samples.zip'
            $unpack=Join-Path $env:TEMP 'joymetric_windows_driver_samples_unpack'
            Invoke-WebRequest -UseBasicParsing -Uri 'https://github.com/microsoft/Windows-driver-samples/archive/refs/heads/main.zip' -OutFile $zip
            if(Test-Path $unpack){Remove-Item $unpack -Recurse -Force}
            Expand-Archive $zip $unpack -Force
            New-Item -ItemType Directory -Force -Path (Join-Path $repo 'audio') | Out-Null
            New-Item -ItemType Directory -Force -Path (Join-Path $repo 'setup') | Out-Null
            Copy-Item (Join-Path $unpack 'Windows-driver-samples-main\audio\simpleaudiosample') (Join-Path $repo 'audio') -Recurse -Force
            Copy-Item (Join-Path $unpack 'Windows-driver-samples-main\setup\devcon') (Join-Path $repo 'setup') -Recurse -Force
            Remove-Item $zip -Force -ErrorAction SilentlyContinue
            Remove-Item $unpack -Recurse -Force -ErrorAction SilentlyContinue
        }
    }
    $upstream = Join-Path $repo 'audio\simpleaudiosample'
    if(-not (Test-Path (Join-Path $upstream 'SimpleAudioSample.sln'))){ throw 'Microsoft SimpleAudioSample source is incomplete.' }

    if($ForceRebuild -or -not (Test-Path (Join-Path $build '.joymetric_patched_v30442'))){
        if(Test-Path $build){Remove-Item $build -Recurse -Force}
        Copy-Item $upstream $build -Recurse -Force
        Write-Host 'Applying JoyMetric render-endpoint patch...' -ForegroundColor Yellow
        & $py (Join-Path $ROOT 'driver\patch_joymetric_driver.py') $build
        if($LASTEXITCODE -ne 0){throw 'JoyMetric driver source patch failed.'}
        Set-Content -Path (Join-Path $build '.joymetric_patched_v30442') -Value 'v30.4.42'
    }

    Write-Host 'Building JoyMetric Virtual Audio Driver (Release x64)...' -ForegroundColor Yellow
    Write-Host 'WDK 28000 command-line verifier workaround: compiling with the broken in-build verifier tasks disabled; explicit x64 verification runs immediately after the build.' -ForegroundColor DarkGray
    & $tools.MsBuild (Join-Path $build 'SimpleAudioSample.sln') /m /t:Rebuild /p:Configuration=Release /p:Platform=x64 /p:SignMode=Off /p:SkipPackageVerification=true /p:ApiValidator_Enable=false /verbosity:minimal | Out-Host
    if($LASTEXITCODE -ne 0){ throw "JoyMetric driver compile/link failed with exit code $LASTEXITCODE." }

    $inf = Get-ChildItem $build -Filter 'SimpleAudioSample.inf' -File -Recurse | Sort-Object LastWriteTime -Descending | Select-Object -First 1
    $sys = Get-ChildItem $build -Filter 'SimpleAudioSample.sys' -File -Recurse | Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if(-not $inf -or -not $sys){ throw 'Built driver package (.inf/.sys) could not be located.' }
    $pkg=$inf.Directory.FullName
    if($sys.Directory.FullName -ne $pkg){ Copy-Item $sys.FullName (Join-Path $pkg $sys.Name) -Force; $sys=Get-Item (Join-Path $pkg $sys.Name) }

    Write-Host 'Validating JoyMetric INF with x64 InfVerif /w...' -ForegroundColor Yellow
    & $tools.InfVerif /w $inf.FullName | Out-Host
    if($LASTEXITCODE -ne 0){ throw "x64 InfVerif /w rejected the JoyMetric INF (exit $LASTEXITCODE)." }

    if($tools.ApiValidationAvailable){
        Write-Host 'Validating JoyMetric x64 driver APIs with x64 ApiValidator...' -ForegroundColor Yellow
        & $tools.ApiValidator ("-DriverPackagePath:{0}" -f $sys.FullName) ("-SupportedApiXmlFiles:{0}" -f $tools.UniversalDDIs) ("-ModuleWhiteListXmlFiles:{0}" -f $tools.ModuleWhiteList) ("-ApiExtractorExePath:{0}" -f $tools.ApiExtractorDir) | Out-Host
        if($LASTEXITCODE -ne 0){ throw "x64 ApiValidator rejected the JoyMetric driver (exit $LASTEXITCODE)." }
    } else {
        Write-Warning 'Skipping ApiValidator only because this installed WDK does not contain a complete x64 validator set. INF verification, catalog generation and signing still run.'
    }

    Write-Host 'Generating JoyMetric driver catalog...' -ForegroundColor Yellow
    & $tools.Inf2Cat /driver:$pkg /os:10_X64 | Out-Host
    if($LASTEXITCODE -ne 0){ throw 'Inf2Cat failed.' }
    $cat=Get-ChildItem $pkg -Filter '*.cat' -File | Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if(-not $cat){throw 'Driver catalog was not generated.'}

    $subject='CN=JoyMetric Development Driver'
    $cert=Get-ChildItem Cert:\LocalMachine\My | Where-Object {$_.Subject -eq $subject -and $_.HasPrivateKey} | Sort-Object NotAfter -Descending | Select-Object -First 1
    if(-not $cert -or $cert.NotAfter -lt (Get-Date).AddDays(30)){
        Write-Host 'Creating local JoyMetric development signing certificate...' -ForegroundColor Yellow
        $cert=New-SelfSignedCertificate -Type CodeSigningCert -Subject $subject -CertStoreLocation 'Cert:\LocalMachine\My' -HashAlgorithm SHA256 -KeyLength 3072 -NotAfter (Get-Date).AddYears(3)
    }
    $cer=Join-Path $driverRoot 'JoyMetricDevelopmentDriver.cer'
    Export-Certificate -Cert $cert -FilePath $cer -Force | Out-Null
    Import-Certificate -FilePath $cer -CertStoreLocation 'Cert:\LocalMachine\Root' | Out-Null
    Import-Certificate -FilePath $cer -CertStoreLocation 'Cert:\LocalMachine\TrustedPublisher' | Out-Null

    Write-Host 'Signing JoyMetric driver package...' -ForegroundColor Yellow
    & $tools.SignTool sign /v /fd SHA256 /s My /sm /sha1 $cert.Thumbprint $sys.FullName | Out-Host
    if($LASTEXITCODE -ne 0){ throw 'Signing JoyMetric .sys failed.' }
    & $tools.Inf2Cat /driver:$pkg /os:10_X64 | Out-Host
    if($LASTEXITCODE -ne 0){ throw 'Inf2Cat refresh failed after .sys signing.' }
    $cat=Get-ChildItem $pkg -Filter '*.cat' -File | Sort-Object LastWriteTime -Descending | Select-Object -First 1
    & $tools.SignTool sign /v /fd SHA256 /s My /sm /sha1 $cert.Thumbprint $cat.FullName | Out-Host
    if($LASTEXITCODE -ne 0){ throw 'Signing JoyMetric catalog failed.' }

    # Build Microsoft's DevCon sample locally instead of assuming a WDK-bundled devcon.exe exists.
    $devconProj=Join-Path $repo 'setup\devcon\devcon.vcxproj'
    Write-Host 'Building local device-enumerator helper...' -ForegroundColor Yellow
    & $tools.MsBuild $devconProj /m /t:Build /p:Configuration=Release /p:Platform=x64 /verbosity:minimal | Out-Host
    if($LASTEXITCODE -ne 0){throw 'DevCon helper build failed.'}
    $devcon=Get-ChildItem (Join-Path $repo 'setup\devcon') -Filter 'devcon.exe' -File -Recurse | Where-Object {$_.FullName -match 'x64|Release'} | Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if(-not $devcon){$devcon=Get-ChildItem (Join-Path $repo 'setup\devcon') -Filter 'devcon.exe' -File -Recurse | Sort-Object LastWriteTime -Descending | Select-Object -First 1}
    if(-not $devcon){throw 'Built DevCon helper could not be located.'}

    Write-Host 'Installing JoyMetric v0.2 render endpoint (Root\JoyMetricVirtualAudio)...' -ForegroundColor Yellow
    # Always replace the old development device. v30.4.42 intentionally
    # restores Microsoft's stock render miniport so Windows Audio Endpoint Builder
    # can create a real output endpoint.
    & $devcon.FullName remove 'Root\JoyMetricVirtualAudio' *> $null
    & $devcon.FullName install $inf.FullName 'Root\JoyMetricVirtualAudio' | Out-Host
    if($LASTEXITCODE -ne 0){ throw "DevCon install failed with exit code $LASTEXITCODE." }
    Start-Sleep -Seconds 5

    $controller=Get-PnpDevice -Class Media -PresentOnly -ErrorAction SilentlyContinue | Where-Object {$_.FriendlyName -match 'JoyMetric Virtual Audio'} | Select-Object -First 1
    $eps=Get-PnpDevice -Class AudioEndpoint -PresentOnly -ErrorAction SilentlyContinue | Where-Object {$_.FriendlyName -match 'JoyMetric'}
    $renderEp=$eps | Where-Object { $_.InstanceId -match '\{0\.0\.0\.' } | Select-Object -First 1
    $captureEp=$eps | Where-Object { $_.InstanceId -match '\{0\.0\.1\.' } | Select-Object -First 1
    if(-not $controller){
        Write-Warning 'Driver package installed but controller has not enumerated yet. Reboot Windows once.'
        Stop-Log
        exit 3010
    }
    if(-not $renderEp){
        Write-Host 'JoyMetric audio endpoints currently visible to Windows:' -ForegroundColor Red
        if($eps){$eps | Select-Object FriendlyName,Status,InstanceId | Format-Table -AutoSize | Out-Host}else{Write-Host '  (none)' -ForegroundColor Red}
        throw 'JOYMETRIC_RENDER_ENDPOINT_MISSING: Windows did not create a JoyMetric render/output endpoint after installing v0.2. The app will not start with a capture-only device.'
    }
    Write-Host ''
    Write-Host 'JoyMetric Virtual Audio Driver v0.2 installed.' -ForegroundColor Green
    Write-Host ('Render/output endpoint: ' + $renderEp.FriendlyName) -ForegroundColor Green
    Set-Content -Path (Join-Path $driverRoot 'installed-v30442.marker') -Value ('v30.4.42 ' + (Get-Date).ToString('o')) -Force
    if($captureEp){Write-Host ('Capture endpoint (unused by Spotify route): ' + $captureEp.FriendlyName) -ForegroundColor DarkGray}
    $eps | Select-Object FriendlyName,Status,InstanceId | Format-Table -AutoSize | Out-Host
    Stop-Log
    exit 0
}
catch {
    Write-Host ''
    Write-Host 'JOYMETRIC DRIVER INSTALL ERROR' -ForegroundColor Red
    Write-Host $_.Exception.Message -ForegroundColor Red
    Write-Host $_.ScriptStackTrace -ForegroundColor DarkRed
    Write-Host "Full log: $LogPath" -ForegroundColor Yellow
    Stop-Log
    exit 1
}
