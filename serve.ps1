# Tiny static file server for the tracker.
# Usage:  powershell -ExecutionPolicy Bypass -File serve.ps1
# Then open http://localhost:8765/ in your browser.  Ctrl+C to stop.

$port = 8765
$root = $PSScriptRoot

$listener = New-Object System.Net.HttpListener
$listener.Prefixes.Add("http://localhost:$port/")
try {
    $listener.Start()
} catch {
    Write-Host "Could not start on port $port. Is something already using it?" -ForegroundColor Red
    exit 1
}

Write-Host "Polymarket Smart Money running at http://localhost:$port/" -ForegroundColor Green
Write-Host "Press Ctrl+C to stop." -ForegroundColor DarkGray

$types = @{
    ".html" = "text/html; charset=utf-8"
    ".js"   = "text/javascript; charset=utf-8"
    ".css"  = "text/css; charset=utf-8"
    ".json" = "application/json; charset=utf-8"
    ".svg"  = "image/svg+xml"
}

while ($listener.IsListening) {
    try {
        $ctx = $listener.GetContext()
    } catch {
        break
    }
    $rel = $ctx.Request.Url.LocalPath.TrimStart("/")
    if ([string]::IsNullOrWhiteSpace($rel)) { $rel = "index.html" }

    # keep the server inside its own folder
    $full = Join-Path $root $rel
    $resolved = [System.IO.Path]::GetFullPath($full)
    if (-not $resolved.StartsWith([System.IO.Path]::GetFullPath($root))) {
        $ctx.Response.StatusCode = 403
        $ctx.Response.Close()
        continue
    }

    if (Test-Path $resolved -PathType Leaf) {
        $bytes = [System.IO.File]::ReadAllBytes($resolved)
        $ext = [System.IO.Path]::GetExtension($resolved).ToLower()
        $ctype = $types[$ext]
        if ($null -eq $ctype) { $ctype = "application/octet-stream" }
        $ctx.Response.ContentType = $ctype
        $ctx.Response.ContentLength64 = $bytes.Length
        $ctx.Response.OutputStream.Write($bytes, 0, $bytes.Length)
    } else {
        $ctx.Response.StatusCode = 404
        $msg = [System.Text.Encoding]::UTF8.GetBytes("Not found: $rel")
        $ctx.Response.OutputStream.Write($msg, 0, $msg.Length)
    }
    $ctx.Response.Close()
}
