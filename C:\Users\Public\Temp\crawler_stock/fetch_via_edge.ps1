
param([string]$Url)
$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$targets = Invoke-RestMethod -Uri "http://127.0.0.1:9222/json/list" -TimeoutSec 5
$page = $targets | Where-Object { $_.type -eq "page" } | Select-Object -First 1
if (-not $page) { Write-Output "NO_PAGE_TARGET"; exit 1 }
$wsUrl = $page.webSocketDebuggerUrl

$ws = [System.Net.WebSockets.ClientWebSocket]::new()
$ct = [System.Threading.CancellationToken]::None
$ws.ConnectAsync([Uri]$wsUrl, $ct).Wait()
if ($ws.State -ne "Open") { Write-Output "WS_CONNECT_FAIL"; exit 1 }

$script:nextId = 100
function Send-Cdp($method, $params) {
    $id = $script:nextId; $script:nextId++
    $obj = @{ id = $id; method = $method }
    if ($params) { $obj.params = $params }
    $json = $obj | ConvertTo-Json -Compress -Depth 8
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($json)
    $seg = [ArraySegment[byte]]::new($bytes)
    $ws.SendAsync($seg, [System.Net.WebSockets.WebSocketMessageType]::Text, $true, $ct).Wait()
    return $id
}
function Read-OneMessage {
    $ms = [System.IO.MemoryStream]::new()
    $buffer = New-Object byte[] 2097152
    do {
        $seg = [ArraySegment[byte]]::new($buffer)
        $res = $ws.ReceiveAsync($seg, $ct).Result
        if ($res.Count -gt 0) { $ms.Write($buffer, 0, $res.Count) }
    } while (-not $res.EndOfMessage)
    return [System.Text.Encoding]::UTF8.GetString($ms.ToArray())
}
function Wait-Response($wantId, $timeoutMs) {
    $deadline = [DateTime]::UtcNow.AddMilliseconds($timeoutMs)
    while ([DateTime]::UtcNow -lt $deadline) {
        if ($ws.State -ne "Open") { return $null }
        $msg = Read-OneMessage
        $obj = $msg | ConvertFrom-Json
        if ($obj.id -eq $wantId) { return $obj }
    }
    return $null
}

$id = Send-Cdp "Page.navigate" @{ url = $Url }
$null = Wait-Response $id 15000

for ($i = 0; $i -lt 24; $i++) {
    Start-Sleep -Milliseconds 500
    $id = Send-Cdp "Runtime.evaluate" @{ expression = "({rs: document.readyState, hasTitle: !!document.querySelector('h1'), hasNextData: !!document.getElementById('__NEXT_DATA__')})"; returnByValue = $true }
    $resp = Wait-Response $id 8000
    $st = $resp.result.result.value
    if ($st.rs -eq "complete" -and $st.hasTitle) { break }
}
Start-Sleep -Milliseconds 1000

$id = Send-Cdp "Runtime.evaluate" @{ expression = "document.documentElement.outerHTML"; returnByValue = $true }
$resp = Wait-Response $id 15000

if ($resp -and $resp.result.result.value) {
    [Console]::Write($resp.result.result.value)
    try { $ws.CloseAsync([System.Net.WebSockets.WebSocketCloseStatus]::NormalClosure, "done", $ct).Wait() } catch {}
    exit 0
} else {
    Write-Output "CDP_EVAL_FAIL"
    try { $ws.CloseAsync([System.Net.WebSockets.WebSocketCloseStatus]::NormalClosure, "done", $ct).Wait() } catch {}
    exit 1
}
