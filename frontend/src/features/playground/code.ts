// "The same request in code": ready-to-run snippets for the OpenAI-compatible endpoint (/v1).

export interface CodeInput {
  baseUrl: string
  /** the real key when there is one; otherwise a placeholder the reader replaces */
  key: string | null
  model: string
  prompt: string
}

export type CodeLang = 'python' | 'curl' | 'js'

export const KEY_PLACEHOLDER = 'sk-ss-...'

function quote(text: string): string {
  return JSON.stringify(text)
}

export function codeSamples(input: CodeInput): Record<CodeLang, string> {
  const key = input.key ?? KEY_PLACEHOLDER
  const model = quote(input.model)
  const prompt = quote(input.prompt)
  const body = JSON.stringify({ model: input.model, messages: [{ role: 'user', content: input.prompt }] })
  return {
    python: `from openai import OpenAI

client = OpenAI(base_url=${quote(input.baseUrl)}, api_key=${quote(key)})
resp = client.chat.completions.create(
    model=${model},
    messages=[{"role": "user", "content": ${prompt}}],
)
print(resp.choices[0].message.content)`,
    curl: `curl ${input.baseUrl}/chat/completions \\
  -H "Authorization: Bearer ${key}" \\
  -H "Content-Type: application/json" \\
  -d '${body.replace(/'/g, "'\\''")}'`,
    js: `import OpenAI from "openai";

const client = new OpenAI({ baseURL: ${quote(input.baseUrl)}, apiKey: ${quote(key)} });
const resp = await client.chat.completions.create({
  model: ${model},
  messages: [{ role: "user", content: ${prompt} }],
});
console.log(resp.choices[0].message.content);`,
  }
}

export function endpoint(): string {
  return `${window.location.origin}/v1`
}
