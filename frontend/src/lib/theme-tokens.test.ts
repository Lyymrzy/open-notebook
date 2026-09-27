import fs from 'node:fs'
import path from 'node:path'

import { describe, expect, it } from 'vitest'

// Tailwind v4 stores its theme tokens as *complete* colour values
// (`--ink-soft: #565a61`, aliased by `@theme inline` as `--muted-foreground`),
// not as the bare HSL triples the Tailwind v3 / shadcn convention used. So a
// token must be consumed as `var(--token)`; wrapping it as `hsl(var(--token))`
// yields an invalid declaration that the browser silently discards.
//
// Silent is the problem: on an SVG `<path stroke="hsl(var(--x))">` the dropped
// declaration leaves the element with no stroke at all, so the knowledge-graph
// tree edges shipped completely invisible while every test stayed green.
// Reviewing for this by eye is unreliable, hence the pinned convention.

const SRC_DIR = path.resolve(__dirname, '..')
const HSL_WRAPPED_VAR = /hsl\(\s*var\(--/g

function sourceFiles(): string[] {
  return (fs.readdirSync(SRC_DIR, { recursive: true }) as string[]).filter(
    f =>
      (f.endsWith('.ts') || f.endsWith('.tsx')) &&
      !f.endsWith('.test.ts') &&
      !f.endsWith('.test.tsx'),
  )
}

describe('theme tokens', () => {
  it('base palette tokens are complete colours, not HSL triples', () => {
    const css = fs.readFileSync(
      path.resolve(SRC_DIR, 'app/globals.css'),
      'utf-8',
    )
    // If this ever fails, tokens became HSL triples and `var()` consumers
    // (inline SVG strokes, React Flow edge styles) must be revisited too.
    for (const token of ['--bg', '--ink-soft', '--line']) {
      const match = css.match(new RegExp(`${token}:\\s*([^;]+);`))
      expect(match, `${token} should be defined in globals.css`).not.toBeNull()
      expect(match?.[1]).toMatch(/^(#|oklch|rgb|color-mix)/)
    }
  })

  it('no source file wraps a theme variable in hsl()', () => {
    const files = sourceFiles()
    // Guard against a vacuous pass if the directory walk ever returns nothing.
    expect(files.length).toBeGreaterThan(50)

    const offenders: string[] = []

    for (const file of files) {
      const full = path.join(SRC_DIR, file)
      const content = fs.readFileSync(full, 'utf-8')
      for (const match of content.matchAll(HSL_WRAPPED_VAR)) {
        const line = content.slice(0, match.index).split('\n').length
        offenders.push(`${file}:${line}`)
      }
    }

    expect(
      offenders,
      `hsl(var(--token)) is invalid here — use var(--token) directly:\n${offenders.join('\n')}`,
    ).toEqual([])
  })

  it('the detector matches the invalid form and ignores the valid one', () => {
    // Keeps the scan above honest: a regex that quietly stopped matching
    // would let this whole suite pass forever.
    const invalid = "stroke: 'hsl(var(--muted-foreground))'"
    const valid = "stroke: 'var(--muted-foreground)'"

    expect(Array.from(invalid.matchAll(HSL_WRAPPED_VAR))).toHaveLength(1)
    expect(Array.from(valid.matchAll(HSL_WRAPPED_VAR))).toHaveLength(0)
  })
})
