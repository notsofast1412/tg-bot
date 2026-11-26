# PaintWatch Ozon Test для Windows x64

Готовый ZIP будет доступен в [тестовом релизе](https://github.com/notsofast1412/tg-bot/releases/tag/ozon-obscura-test-0.4.11), если GitHub Actions опубликует его.

Чтобы скачать и собрать архив из восьми частей, выполните в PowerShell:

```powershell
$url = 'https://raw.githubusercontent.com/notsofast1412/tg-bot/ozon-obscura-test-0.4.11-download'
$zip = 'PaintWatch-Ozon-Test-0.4.11-obscura-0.2.7-win-x64.zip'
$out = [IO.File]::Create($zip)
try {
    for ($i = 0; $i -lt 8; $i++) {
        $part = 'archive-part-{0:D2}.bin' -f $i
        Invoke-WebRequest "$url/$part" -OutFile $part -ErrorAction Stop
        $in = [IO.File]::OpenRead($part)
        try { $in.CopyTo($out) } finally { $in.Dispose() }
    }
} finally { $out.Dispose() }
if ((Get-FileHash $zip).Hash.ToLowerInvariant() -ne '7e2eb55339bd840bee43ba229fc4eac26fa4630cf068e09ae968af1f27b377d0') { throw 'SHA-256 архива не совпадает' }
```

Отдельный экспериментальный стенд PaintWatch для Windows x64. В комплект входят Obscura 0.2.7, Python 3.12.10 и Playwright 1.55.0.

Распакуйте ZIP целиком и запустите из папки комплекта:

```powershell
.\run.cmd --fixtures
.\run.cmd --ozon "https://www.ozon.ru/product/196525150/"
```

В облачной Linux-среде прошли 12 из 12 фикстур. Запуск комплекта под Windows и получение цены с реальной карточки ещё нужно подтвердить. Тег указывает на базовый коммит PaintWatch. Код стенда находится в ZIP.

SHA-256 ZIP: `7e2eb55339bd840bee43ba229fc4eac26fa4630cf068e09ae968af1f27b377d0`.

