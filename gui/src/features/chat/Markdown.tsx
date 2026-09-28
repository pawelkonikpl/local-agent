import { useState } from 'react'
import type { ComponentPropsWithoutRef, MouseEvent } from 'react'
import ReactMarkdown, { type ExtraProps } from 'react-markdown'
import remarkGfm from 'remark-gfm'
import remarkBreaks from 'remark-breaks'
import rehypeHighlight from 'rehype-highlight'

function languageFromNode(node: ExtraProps['node']): string | undefined {
  const codeNode = node?.children.find((child) => child.type === 'element' && child.tagName === 'code')
  if (!codeNode || codeNode.type !== 'element') return undefined
  const className = codeNode.properties.className
  const classes = Array.isArray(className) ? className : []
  const languageClass = classes.find((c) => typeof c === 'string' && c.startsWith('language-'))
  return typeof languageClass === 'string' ? languageClass.slice('language-'.length) : undefined
}

function CodeBlock({ node, children, ...props }: ComponentPropsWithoutRef<'pre'> & ExtraProps) {
  const [copied, setCopied] = useState(false)
  const language = languageFromNode(node)

  async function handleCopy(event: MouseEvent<HTMLButtonElement>) {
    const pre = event.currentTarget.closest('.code-block')?.querySelector('pre')
    const text = pre?.textContent ?? ''
    await navigator.clipboard.writeText(text)
    setCopied(true)
    setTimeout(() => setCopied(false), 1500)
  }

  return (
    <div className="code-block">
      <div className="code-block-header">
        <span className="code-block-lang">{language ?? 'text'}</span>
        <button type="button" className="code-block-copy" onClick={handleCopy}>
          {copied ? 'Copied' : 'Copy'}
        </button>
      </div>
      <pre {...props}>{children}</pre>
    </div>
  )
}

function hostnameOf(url: string | undefined): string {
  if (!url) return ''
  try {
    return new URL(url, window.location.href).hostname
  } catch {
    return ''
  }
}

/**
 * External images are never fetched: loading one sends a request the page chose, and its URL can
 * carry data out of the conversation (the EchoLeak pattern). Shown as text instead.
 */
function BlockedImage({ src, alt }: ComponentPropsWithoutRef<'img'> & ExtraProps) {
  const host = hostnameOf(typeof src === 'string' ? src : undefined)
  return (
    <span className="md-image-blocked" title={typeof src === 'string' ? src : undefined}>
      [image: {alt || 'no description'}
      {host ? ` — ${host}` : ''}]
    </span>
  )
}

/**
 * Links open in a new tab without a referrer, and name the host they lead to whenever the link
 * text doesn't, so a link can't pass itself off as pointing somewhere else.
 */
function SafeLink({ node: _node, href, children, ...props }: ComponentPropsWithoutRef<'a'> & ExtraProps) {
  const host = hostnameOf(href)
  const text = typeof children === 'string' ? children : ''
  return (
    <>
      <a {...props} href={href} title={href} target="_blank" rel="noopener noreferrer nofollow">
        {children}
      </a>
      {host && text !== host && !text.includes(host) ? <span className="md-link-host"> ({host})</span> : null}
    </>
  )
}

interface MarkdownProps {
  text: string
}

export default function Markdown({ text }: MarkdownProps) {
  return (
    <div className="markdown-body">
      <ReactMarkdown
        remarkPlugins={[remarkGfm, remarkBreaks]}
        rehypePlugins={[rehypeHighlight]}
        components={{ pre: CodeBlock, img: BlockedImage, a: SafeLink }}
      >
        {text}
      </ReactMarkdown>
    </div>
  )
}
