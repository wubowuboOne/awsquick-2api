// Windows Credential Manager reader/writer for the Quick desktop app's
// stored session. The app persists its OAuth session as generic credentials
// named `quickwork-<provider>-<profile>`; blobs larger than ~2.5 KB are split
// into `…_chunk_N` entries behind a {__chunks__, __sha__} manifest. Values are
// UTF-16LE JSON — readable by the owning user via CredRead, no DPAPI step.
//
// One PowerShell host does enumerate+read (and a second mode does chunked
// write-back) so the plugin stays zero-dependency; each call costs ~200 ms
// and results are cached per process.
import { execFile } from 'node:child_process'
import { mkdtempSync, writeFileSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import path from 'node:path'
import { createHash } from 'node:crypto'

const PS_READ = String.raw`
$ErrorActionPreference = 'Stop'
$src = @'
using System;
using System.Runtime.InteropServices;
using System.Collections.Generic;
public class CM {
  [StructLayout(LayoutKind.Sequential, CharSet=CharSet.Unicode)]
  public struct CREDENTIAL {
    public int Flags; public int Type;
    public string TargetName; public string Comment;
    public System.Runtime.InteropServices.ComTypes.FILETIME LastWritten;
    public int CredentialBlobSize; public IntPtr CredentialBlob;
    public int Persist; public int AttributeCount; public IntPtr Attributes;
    public string TargetAlias; public string UserName;
  }
  [DllImport("advapi32.dll", SetLastError=true, CharSet=CharSet.Unicode)]
  public static extern bool CredRead(string target, int type, int flags, out IntPtr cred);
  [DllImport("advapi32.dll", SetLastError=true, CharSet=CharSet.Unicode)]
  public static extern bool CredEnumerate(string filter, int flags, out int count, out IntPtr creds);
  [DllImport("advapi32.dll")] public static extern void CredFree(IntPtr cred);
  public static string Read(string t) {
    IntPtr p; if (!CredRead(t, 1, 0, out p)) return null;
    var c = (CREDENTIAL)Marshal.PtrToStructure(p, typeof(CREDENTIAL));
    var b = new byte[c.CredentialBlobSize];
    Marshal.Copy(c.CredentialBlob, b, 0, b.Length); CredFree(p);
    return System.Text.Encoding.Unicode.GetString(b);
  }
  public static List<string> Enum(string prefix) {
    int n; IntPtr arr; var list = new List<string>();
    // CredEnumerate's filter does not prefix-match as documented; enumerate
    // all and filter client-side instead.
    if (!CredEnumerate(null, 0, out n, out arr)) return list;
    for (int i = 0; i < n; i++) {
      IntPtr p = Marshal.ReadIntPtr(arr, i * IntPtr.Size);
      var c = (CREDENTIAL)Marshal.PtrToStructure(p, typeof(CREDENTIAL));
      if (c.TargetName != null && c.TargetName.StartsWith(prefix)) list.Add(c.TargetName);
    }
    CredFree(arr); return list;
  }
}
'@
Add-Type -TypeDefinition $src -Language CSharp
$out = @()
foreach ($t in [CM]::Enum('quickwork-')) {
  if ($t -match '_chunk_' -or $t.StartsWith('session@')) { continue }
  $raw = [CM]::Read($t)
  if ($null -eq $raw) { continue }
  $json = $raw
  if ($raw -match '"__chunks__"\s*:\s*(\d+)') {
    $n = [int]$Matches[1]; $sb = New-Object Text.StringBuilder
    for ($i = 0; $i -lt $n; $i++) {
      $part = [CM]::Read($t + '_chunk_' + $i)
      if ($null -eq $part) { break }
      [void]$sb.Append($part)
    }
    $json = $sb.ToString()
  }
  try { $obj = $json | ConvertFrom-Json } catch { continue }
  if ($obj.refresh_token) {
    $out += [pscustomobject]@{ target = $t; data = $obj }
  }
}
if ($out.Count -eq 0) { '[]' }
elseif ($out.Count -eq 1) { $out[0] | ConvertTo-Json -Depth 8 -Compress }
else { ,$out | ConvertTo-Json -Depth 8 -Compress }
`

const PS_WRITE = String.raw`
$ErrorActionPreference = 'Stop'
$Target = __TARGET__
$JsonFile = __JSONFILE__
$src = @'
using System;
using System.Runtime.InteropServices;
public class CMW {
  [StructLayout(LayoutKind.Sequential, CharSet=CharSet.Unicode)]
  public struct CREDENTIAL {
    public int Flags; public int Type;
    public string TargetName; public string Comment;
    public System.Runtime.InteropServices.ComTypes.FILETIME LastWritten;
    public int CredentialBlobSize; public IntPtr CredentialBlob;
    public int Persist; public int AttributeCount; public IntPtr Attributes;
    public string TargetAlias; public string UserName;
  }
  [DllImport("advapi32.dll", SetLastError=true, CharSet=CharSet.Unicode)]
  public static extern bool CredWrite(ref CREDENTIAL cred, int flags);
  [DllImport("advapi32.dll", SetLastError=true, CharSet=CharSet.Unicode)]
  public static extern bool CredDelete(string target, int type, int flags);
  [DllImport("advapi32.dll", SetLastError=true, CharSet=CharSet.Unicode)]
  public static extern bool CredRead(string target, int type, int flags, out IntPtr cred);
  public static bool Exists(string t) { IntPtr p; bool ok = CredRead(t, 1, 0, out p); if (ok) CredFree(p); return ok; }
  [DllImport("advapi32.dll")] public static extern void CredFree(IntPtr cred);
  public static bool Write(string t, byte[] blob) {
    if (blob.Length > 2560) return false;
    var c = new CREDENTIAL();
    c.Type = 1; c.TargetName = t; c.UserName = "session"; c.Persist = 2;
    c.CredentialBlobSize = blob.Length;
    c.CredentialBlob = Marshal.AllocHGlobal(blob.Length);
    Marshal.Copy(blob, 0, c.CredentialBlob, blob.Length);
    bool ok = CredWrite(ref c, 0);
    Marshal.FreeHGlobal(c.CredentialBlob);
    return ok;
  }
  public static bool Delete(string t) { return CredDelete(t, 1, 0); }
}
'@
Add-Type -TypeDefinition $src -Language CSharp
$json = [IO.File]::ReadAllText($JsonFile, [Text.Encoding]::UTF8)
$sha = [BitConverter]::ToString([Security.Cryptography.SHA256]::Create().ComputeHash([Text.Encoding]::UTF8.GetBytes($json))).Replace('-','').ToLower()
# CredWrite blobs cap at 2560 bytes; chunk the UTF-16 payload at 2400 bytes
# (1200 code units), never splitting a surrogate pair.
$chars = $json.ToCharArray()
$maxChars = 1200
$parts = @()
$pos = 0
while ($pos -lt $chars.Length) {
  $len = [Math]::Min($maxChars, $chars.Length - $pos)
  if ($pos + $len -lt $chars.Length) {
    $u = [int][char]$chars[$pos + $len - 1]
    if ($u -ge 0xD800 -and $u -le 0xDBFF) { $len -= 1 }
  }
  if ($len -lt 1) { $len = [Math]::Min(2, $chars.Length - $pos) }
  $parts += ,([Text.Encoding]::Unicode.GetBytes(-join $chars[$pos..($pos + $len - 1)]))
  $pos += $len
}
$manifest = ('{"__chunks__": ' + $parts.Count + ', "__sha__": "' + $sha + '"}')
function Write-Target([string]$t) {
  [void][CMW]::Write($t, [Text.Encoding]::Unicode.GetBytes($manifest))
  for ($i = 0; $i -lt $parts.Count; $i++) { [void][CMW]::Write($t + '_chunk_' + $i, $parts[$i]) }
  for ($i = $parts.Count; $i -lt 16; $i++) { [void][CMW]::Delete($t + '_chunk_' + $i) }
}
Write-Target $Target
# The app keeps a session@-prefixed twin of the same blob; update it too so
# whichever component reads it sees the rotated tokens.
$twin = 'session@' + $Target
if ([CMW]::Exists($twin)) { Write-Target $twin }
Write-Output 'ok'
`

const encode = (script) => Buffer.from(script, 'utf16le').toString('base64')

function runPs(args, { timeoutMs = 20000 } = {}) {
  return new Promise((resolve, reject) => {
    execFile('powershell.exe', ['-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', ...args],
      { timeout: timeoutMs, windowsHide: true, maxBuffer: 8 * 1024 * 1024 },
      (err, stdout, stderr) => {
        if (err) return reject(new Error(`powershell failed: ${stderr || err.message}`))
        resolve(stdout)
      })
  })
}

let cache = null

/**
 * Read every `quickwork-*` credential that contains a refresh_token.
 * Resolves [{target, data}] where data is the parsed session JSON.
 * Returns [] when the app was never signed in (or not on Windows).
 */
export async function readQuickCredentials({ fresh = false } = {}) {
  if (process.platform !== 'win32') return []
  if (cache && !fresh) return cache
  const out = await runPs(['-EncodedCommand', encode(PS_READ)])
  const text = out.trim()
  if (!text || text === '[]') { cache = []; return cache }
  let parsed
  try { parsed = JSON.parse(text) } catch { parsed = [] }
  cache = (Array.isArray(parsed) ? parsed : [parsed])
    .filter((c) => c && typeof c === 'object' && c.target && c.data?.refresh_token)
  return cache
}

/**
 * Write a refreshed session blob back to the same credential target so the
 * desktop app keeps working with the rotated tokens. `data` must be the full
 * session JSON object the app expects (we persist our normalized record —
 * extra fields are harmless to the app's reader, missing required ones are
 * not; callers pass through the original blob merged with new tokens).
 */
export async function writeQuickCredential(target, data) {
  if (process.platform !== 'win32') return false
  const dir = mkdtempSync(path.join(tmpdir(), 'qw-cred-'))
  const file = path.join(dir, 'session.json')
  try {
    writeFileSync(file, JSON.stringify(data))
    const script = PS_WRITE
      .replace('__TARGET__', JSON.stringify(target))
      .replace('__JSONFILE__', JSON.stringify(file))
    const out = await runPs(['-EncodedCommand', encode(script)])
    return out.trim() === 'ok'
  } finally {
    try { rmSync(dir, { recursive: true, force: true }) } catch {}
  }
}
