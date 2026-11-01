import { Fragment, type ReactNode } from 'react';

// 她的作品是她自己写的 Markdown。这里只认最常用的几样（标题、段落、列表、引用、
// 粗体、斜体、行内代码、分隔线），而且一律生成 React 元素、不拼 HTML：
// 作品里写了什么都只会被当成文字显示。

function inline(text: string, key: string): ReactNode[] {
  const out: ReactNode[] = [];
  const re = /(\*\*[^*]+\*\*|\*[^*\s][^*]*\*|`[^`]+`)/g;
  let last = 0;
  let m: RegExpExecArray | null;
  let i = 0;
  while ((m = re.exec(text))) {
    if (m.index > last) out.push(text.slice(last, m.index));
    const token = m[0];
    const k = `${key}-${i++}`;
    if (token.startsWith('**')) out.push(<strong key={k}>{token.slice(2, -2)}</strong>);
    else if (token.startsWith('`')) out.push(<code key={k}>{token.slice(1, -1)}</code>);
    else out.push(<em key={k}>{token.slice(1, -1)}</em>);
    last = m.index + token.length;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

export function Markdown({ source }: { source: string }) {
  const lines = source.replace(/\r\n/g, '\n').split('\n');
  const blocks: ReactNode[] = [];
  let para: string[] = [];
  let list: { ordered: boolean; items: string[] } | null = null;
  let quote: string[] = [];

  const flush = () => {
    const k = `b${blocks.length}`;
    if (para.length) {
      blocks.push(<p key={k}>{inline(para.join(' '), k)}</p>);
      para = [];
    } else if (list) {
      const items = list.items.map((item, i) => <li key={i}>{inline(item, `${k}-${i}`)}</li>);
      blocks.push(list.ordered ? <ol key={k}>{items}</ol> : <ul key={k}>{items}</ul>);
      list = null;
    } else if (quote.length) {
      blocks.push(<blockquote key={k}>{inline(quote.join(' '), k)}</blockquote>);
      quote = [];
    }
  };

  for (const raw of lines) {
    const line = raw.trimEnd();
    const heading = /^(#{1,4})\s+(.*)$/.exec(line);
    const bullet = /^\s*[-*+]\s+(.*)$/.exec(line);
    const numbered = /^\s*\d+[.)]\s+(.*)$/.exec(line);
    const quoted = /^>\s?(.*)$/.exec(line);
    if (!line.trim()) {
      flush();
    } else if (heading) {
      flush();
      const level = heading[1].length;
      const k = `b${blocks.length}`;
      const content = inline(heading[2], k);
      blocks.push(
        level === 1 ? (
          <h2 key={k}>{content}</h2>
        ) : level === 2 ? (
          <h3 key={k}>{content}</h3>
        ) : (
          <h4 key={k}>{content}</h4>
        ),
      );
    } else if (/^(-{3,}|\*{3,})$/.test(line.trim())) {
      flush();
      blocks.push(<hr key={`b${blocks.length}`} />);
    } else if (bullet || numbered) {
      const ordered = !bullet;
      if (para.length || quote.length || (list && list.ordered !== ordered)) flush();
      if (!list) list = { ordered, items: [] };
      list.items.push((bullet ?? numbered)![1]);
    } else if (quoted) {
      if (para.length || list) flush();
      quote.push(quoted[1]);
    } else {
      if (list || quote.length) flush();
      para.push(line.trim());
    }
  }
  flush();
  return <Fragment>{blocks}</Fragment>;
}
