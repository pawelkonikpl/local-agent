import { useEffect, useState } from 'react'
import { listModels, type ChatModel } from '../../api/client'

const STORAGE_KEY = 'la:chat-model'
const EFFORTS_STORAGE_KEY = 'la:chat-reasoning-efforts'

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

function readStoredEfforts(): Record<string, string> {
  try {
    const parsed = JSON.parse(localStorage.getItem(EFFORTS_STORAGE_KEY) ?? '{}')
    return parsed && typeof parsed === 'object' ? parsed : {}
  } catch {
    return {}
  }
}

function writeStoredEfforts(efforts: Record<string, string>) {
  try {
    localStorage.setItem(EFFORTS_STORAGE_KEY, JSON.stringify(efforts))
  } catch {
    // Same as the model: the choice just won't persist.
  }
}

/**
 * The models offered by `GET /models`, the one picked for the next message and its reasoning
 * level. Both picks are remembered per browser -- the reasoning level per model, since each model
 * accepts its own set; a stored value the server no longer offers falls back to the default.
 * `model` stays `undefined` until the list loads (or if it fails), which makes the API use its
 * default, so sending never waits on this. `reasoningEffort` is `undefined` for "provider default"
 * and whenever the model offers no reasoning setting (`reasoningEfforts` is then empty).
 */
export function useChatModel() {
  const [models, setModels] = useState<ChatModel[]>([])
  const [model, setModel] = useState<string | undefined>(undefined)
  const [efforts, setEfforts] = useState<Record<string, string>>(readStoredEfforts)

  useEffect(() => {
    let cancelled = false
    listModels()
      .then((result) => {
        if (cancelled) return
        const stored = readStoredModel()
        setModels(result.models)
        setModel(stored && result.models.some((m) => m.id === stored) ? stored : result.default)
      })
      .catch(() => {
        // Best-effort, like token usage: without the list the picker is hidden.
      })
    return () => {
      cancelled = true
    }
  }, [])

  const reasoningEfforts = models.find((m) => m.id === model)?.reasoning_efforts ?? []
  const storedEffort = model ? efforts[model] : undefined
  const reasoningEffort = storedEffort && reasoningEfforts.includes(storedEffort) ? storedEffort : undefined

  function selectModel(next: string) {
    setModel(next)
    writeStoredModel(next)
  }

  function selectReasoningEffort(next: string | undefined) {
    if (!model) return
    const updated = { ...efforts }
    if (next) updated[model] = next
    else delete updated[model]
    setEfforts(updated)
    writeStoredEfforts(updated)
  }

  return { models, model, selectModel, reasoningEfforts, reasoningEffort, selectReasoningEffort }
}
