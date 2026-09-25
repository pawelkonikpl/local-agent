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

interface MarkdownProps {
  text: string
}

export default function Markdown({ text }: MarkdownProps) {
  return (
    <div className="markdown-body">
      <ReactMarkdown
        remarkPlugins={[remarkGfm, remarkBreaks]}
        rehypePlugins={[rehypeHighlight]}
        components={{ pre: CodeBlock }}
      >
        {text}
      </ReactMarkdown>
    </div>
  )
}
