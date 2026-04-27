$f="potsworks-pisowifi/overlay/opt/pisowifi/admin/index.html"
$sw2=[System.IO.StreamWriter]::new($f,$false,[System.Text.Encoding]::UTF8)
function w($x){$sw2.WriteLine($x)}
w "<!DOCTYPE html>"
w "<html lang='fil'>"
w "<head>"
w "<meta charset='UTF-8'>"
w "<meta name='viewport' content='width=device-width,initial-scale=1,maximum-scale=1'>"
w "<title>PotsWorks PisoWifi - Admin</title>"
w "<style>"
w ":root{--bg:#0f1117;--s1:#1a1d27;--s2:#22263a;--bd:#2e3350;--ac:#4f8ef7;--ac2:#7c5cfc;--gr:#22c55e;--yw:#f59e0b;--rd:#ef4444;--tx:#e8eaf6;--tx2:#8b90b0;--r:12px;--rs:8px;--fn:'Segoe UI',system-ui,sans-serif}"
w "*{box-sizing:border-box;margin:0;padding:0}"
w "body{background:var(--bg);color:var(--tx);font-family:var(--fn);min-height:100vh;display:flex;overflow-x:hidden}"
w "a{text-decoration:none;color:inherit}"
