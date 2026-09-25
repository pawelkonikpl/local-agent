const STORAGE_KEY = 'la:session-titles'
const MAX_TITLE_WORDS = 6
const MAX_TITLE_LENGTH = 48

function readStore(): Record<string, string> {
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    return raw ? JSON.parse(raw) : {}
  } catch {
    return {}
  }
}

function writeStore(titles: Record<string, string>) {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(titles))
  } catch {
    // Storage can be unavailable (private browsing, quota); the title just won't persist.
  }
}

export function loadSessionTitles(): Record<string, string> {
  return readStore()
}

export function saveSessionTitle(sessionId: string, title: string): void {
  const titles = readStore()
  titles[sessionId] = title
  writeStore(titles)
}

/** Turns a message's opening words into a short session title, e.g. for a sidebar list entry. */
export function deriveTitle(text: string): string {
  const trimmed = text.trim().replace(/\s+/g, ' ')
  const words = trimmed.split(' ')
  let title = words.slice(0, MAX_TITLE_WORDS).join(' ')
  if (title.length > MAX_TITLE_LENGTH) {
    title = title.slice(0, MAX_TITLE_LENGTH).trimEnd()
  }
  if (title.length < trimmed.length) {
    title += '…'
  }
  return title
}
