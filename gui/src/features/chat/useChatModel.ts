import { useEffect, useState } from 'react'
import { listModels } from '../../api/client'

const STORAGE_KEY = 'la:chat-model'

function readStoredModel(): string | null {
  try {
    return localStorage.getItem(STORAGE_KEY)
  } catch {
    return null
  }
}

function writeStoredModel(model: string) {
  try {
    localStorage.setItem(STORAGE_KEY, model)
  } catch {
    // Storage can be unavailable (private browsing, quota); the choice just won't persist.
  }
}

/**
 * The models offered by `GET /models` and the one picked for the next message. The pick is
 * remembered per browser; a stored model the server no longer offers falls back to its default.
 * `model` stays `undefined` until the list loads (or if it fails), which makes the API use its
 * default, so sending never waits on this.
 */
export function useChatModel() {
  const [models, setModels] = useState<string[]>([])
  const [model, setModel] = useState<string | undefined>(undefined)

  useEffect(() => {
    let cancelled = false
    listModels()
      .then((result) => {
        if (cancelled) return
        const stored = readStoredModel()
        setModels(result.models)
        setModel(stored && result.models.includes(stored) ? stored : result.default)
      })
      .catch(() => {
        // Best-effort, like token usage: without the list the picker is hidden.
      })
    return () => {
      cancelled = true
    }
  }, [])

  function selectModel(next: string) {
    setModel(next)
    writeStoredModel(next)
  }

  return { models, model, selectModel }
}
