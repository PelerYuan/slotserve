// An SSH key pair made in the browser (Web Crypto Ed25519): the private key is downloaded once and never sent to
// the server, which only gets the public key (F03 "generate a key pair").

export interface KeyPair {
  /** "ssh-ed25519 AAAA… comment" */
  publicKey: string
  /** OpenSSH private key file (unencrypted) */
  privateKey: string
}

/** Web Crypto Ed25519 needs a secure context (https or localhost) and a recent browser. */
export async function canGenerateKeys(): Promise<boolean> {
  if (!globalThis.isSecureContext || !globalThis.crypto?.subtle) return false
  try {
    await crypto.subtle.generateKey({ name: 'Ed25519' }, true, ['sign', 'verify'])
    return true
  } catch {
    return false
  }
}

const enc = new TextEncoder()

function u32(n: number): Uint8Array {
  return new Uint8Array([(n >>> 24) & 255, (n >>> 16) & 255, (n >>> 8) & 255, n & 255])
}

function concat(...parts: Uint8Array[]): Uint8Array {
  const out = new Uint8Array(parts.reduce((n, p) => n + p.length, 0))
  let i = 0
  for (const p of parts) {
    out.set(p, i)
    i += p.length
  }
  return out
}

/** SSH wire format string: length + bytes. */
function str(data: Uint8Array | string): Uint8Array {
  const bytes = typeof data === 'string' ? enc.encode(data) : data
  return concat(u32(bytes.length), bytes)
}

function base64(bytes: Uint8Array): string {
  let s = ''
  for (const b of bytes) s += String.fromCharCode(b)
  return btoa(s)
}

/** The OpenSSH encodings of an Ed25519 key from its 32-byte seed and public key. */
export function openSshKeys(seed: Uint8Array, pub: Uint8Array, comment: string, check = 0): KeyPair {
  const type = 'ssh-ed25519'
  const pubBlob = concat(str(type), str(pub))
  let priv = concat(u32(check), u32(check), str(type), str(pub), str(concat(seed, pub)), str(comment))
  const pad: number[] = []
  for (let i = 1; (priv.length + pad.length) % 8 !== 0; i++) pad.push(i)
  priv = concat(priv, new Uint8Array(pad))
  const file = concat(enc.encode('openssh-key-v1\0'), str('none'), str('none'), str(''), u32(1), str(pubBlob), str(priv))
  const lines = base64(file).match(/.{1,70}/g) ?? []
  return {
    publicKey: `${type} ${base64(pubBlob)} ${comment}`.trim(),
    privateKey: `-----BEGIN OPENSSH PRIVATE KEY-----\n${lines.join('\n')}\n-----END OPENSSH PRIVATE KEY-----\n`,
  }
}

export async function generateKeyPair(comment: string): Promise<KeyPair> {
  const pair = (await crypto.subtle.generateKey({ name: 'Ed25519' }, true, ['sign', 'verify'])) as CryptoKeyPair
  const pub = new Uint8Array(await crypto.subtle.exportKey('raw', pair.publicKey))
  const pkcs8 = new Uint8Array(await crypto.subtle.exportKey('pkcs8', pair.privateKey))
  const seed = pkcs8.slice(pkcs8.length - 32) // PKCS#8 Ed25519: a fixed 16-byte header, then the 32-byte seed
  const check = crypto.getRandomValues(new Uint32Array(1))[0]
  return openSshKeys(seed, pub, comment, check)
}

/** Offers a text file for download (the private key: once, never stored anywhere by SlotServe). */
export function download(name: string, text: string): void {
  const url = URL.createObjectURL(new Blob([text], { type: 'application/octet-stream' }))
  const a = document.createElement('a')
  a.href = url
  a.download = name
  document.body.appendChild(a)
  a.click()
  a.remove()
  window.setTimeout(() => URL.revokeObjectURL(url), 1000)
}
