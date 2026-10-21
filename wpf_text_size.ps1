# Measures text the way WPF lays it out. Prints {"w": width, "h": height} in the units
# of EmSize (pass the font size in layout units, i.e. points * 5 at 360 dpi).
# Text is passed base64-encoded (UTF-8) to survive the command line.
param([string]$FontFile, [string]$Family, [double]$EmSize, [string]$TextBase64)
Add-Type -AssemblyName PresentationCore
$text = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($TextBase64))
if ($FontFile -and (Test-Path $FontFile)) {
    $dir = [IO.Path]::GetDirectoryName((Resolve-Path $FontFile).Path) + '\'
    $font = New-Object Windows.Media.FontFamily ([Uri]$dir), ('./#' + $Family)
} else {
    $font = New-Object Windows.Media.FontFamily $Family
}
$typeface = New-Object Windows.Media.Typeface $font, ([Windows.FontStyles]::Normal), ([Windows.FontWeights]::Normal), ([Windows.FontStretches]::Normal)
$ft = New-Object Windows.Media.FormattedText $text, ([Globalization.CultureInfo]::InvariantCulture),
    ([Windows.FlowDirection]::LeftToRight), $typeface, $EmSize, ([Windows.Media.Brushes]::Black), 1.0
[pscustomobject]@{ w = $ft.WidthIncludingTrailingWhitespace; h = $ft.Height } | ConvertTo-Json -Compress
