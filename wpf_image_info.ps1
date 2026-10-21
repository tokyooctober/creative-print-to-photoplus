# Prints image geometry exactly as Epson Photo+ measures it (WPF BitmapFrame).
# Output: one JSON object per line {path, pw, ph, w, h, orientation}
param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Paths)
Add-Type -AssemblyName PresentationCore
foreach ($p in $Paths) {
    $fs = [System.IO.File]::OpenRead($p)
    try {
        $frame = [System.Windows.Media.Imaging.BitmapFrame]::Create($fs,
            [System.Windows.Media.Imaging.BitmapCreateOptions]::DelayCreation,
            [System.Windows.Media.Imaging.BitmapCacheOption]::None)
        $orientation = 1
        try {
            $meta = $frame.Metadata
            if ($meta -ne $null) {
                $v = $meta.GetQuery('System.Photo.Orientation')
                if ($v -ne $null) { $orientation = [int]$v }
            }
        } catch {}
        [pscustomobject]@{
            path = $p; pw = $frame.PixelWidth; ph = $frame.PixelHeight
            w = $frame.Width; h = $frame.Height; orientation = $orientation
        } | ConvertTo-Json -Compress
    } catch {
        [pscustomobject]@{ path = $p; error = $_.Exception.Message } | ConvertTo-Json -Compress
    } finally {
        $fs.Close()
    }
}
