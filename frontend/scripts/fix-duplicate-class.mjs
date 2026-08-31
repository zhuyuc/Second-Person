import { readFileSync, writeFileSync } from 'fs'

const files = ['src/views/MemoryView.vue', 'src/views/SettingsView.vue']

for (const file of files) {
  let src = readFileSync(file, 'utf8')
  // Merge duplicate class attributes: class="a" class="b" -> class="a b"
  src = src.replace(/class="([^"]*)"\s+class="([^"]*)"/g, (_, a, b) => `class="${a} ${b}"`)
  // Repeat until no more duplicates (nested replacements)
  let prev
  do {
    prev = src
    src = src.replace(/class="([^"]*)"\s+class="([^"]*)"/g, (_, a, b) => `class="${a} ${b}"`)
  } while (src !== prev)
  writeFileSync(file, src)
  const dup = (src.match(/class="[^"]*"\s+class="/g) || []).length
  console.log(`${file}: ${dup} duplicate class attrs remaining`)
}
