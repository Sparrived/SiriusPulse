import { useEffect, useState } from 'react';
import { useToolHistory } from '@/lib/queries-house';
import { ago } from '@/lib/time';
import { toolName } from './names';

const SIZE = 30;

/** 用过的记录：她什么时候、为谁、用了什么，花了多久，成没成 */
export function History() {
  const [tool, setTool] = useState('');
  const [success, setSuccess] = useState('');
  const [page, setPage] = useState(0);
  const { data, isFetching } = useToolHistory({ tool, success }, page, SIZE);
  useEffect(() => setPage(0), [tool, success]);
  const stats = Object.entries(data?.stats ?? {}).sort((a, b) => b[1].calls - a[1].calls);
  const total = data?.total ?? 0;

  return (
    <>
      <div className="usage-tiles">
        {stats.map(([name, s]) => (
          <button
            key={name}
            className={`usage-tile paper${tool === name ? ' is-on' : ''}`}
            onClick={() => setTool(tool === name ? '' : name)}
          >
            <span className="hand">{toolName(name)}</span>
            <b className="num">{s.calls}</b>
            <em>
              成功 {Math.round((s.successes / Math.max(1, s.calls)) * 100)}% · 平均{' '}
              {(s.avg_ms / 1000).toFixed(1)} 秒
            </em>
          </button>
        ))}
      </div>

      <section className="sheet paper">
        <header className="sheet__head">
          <h2 className="hand">{tool ? `「${toolName(tool)}」的记录` : '最近用过的'}</h2>
          <select
            className="input history__filter"
            value={success}
            onChange={(e) => setSuccess(e.target.value)}
          >
            <option value="">全部</option>
            <option value="true">成功的</option>
            <option value="false">失败的</option>
          </select>
          <span className="field__hint">{isFetching ? '翻找中……' : `${total} 次`}</span>
        </header>
        {!data?.history.length && <p className="field__hint">还没有记录。</p>}
        <ul className="history">
          {data?.history.map((h, i) => (
            <li key={`${h.timestamp}-${i}`} className={h.success ? '' : 'is-fail'}>
              <details>
                <summary>
                  <i className={`pf-dot${h.success ? ' is-on' : ''}`} />
                  <span className="hand">{toolName(h.tool_name)}</span>
                  <span className="history__what">{h.error || h.result_summary || '（没有留下结果）'}</span>
                  <em>
                    {ago(new Date(h.timestamp * 1000).toISOString())} · {(h.duration_ms / 1000).toFixed(1)}s
                  </em>
                </summary>
                <div className="history__detail">
                  <p className="field__hint">
                    为 {h.caller_user_id === 'self' ? '她自己' : h.caller_user_id || '某人'} ·{' '}
                    {new Date(h.timestamp * 1000).toLocaleString('zh-CN')}
                  </p>
                  {h.params && <pre>{JSON.stringify(h.params, null, 2)}</pre>}
                  {h.result_summary && <pre className="work__output">{h.result_summary}</pre>}
                  {h.error && <pre className="history__error">{h.error}</pre>}
                </div>
              </details>
            </li>
          ))}
        </ul>
        {total > SIZE && (
          <div className="history__pager">
            <button className="chatlog__more" disabled={page === 0} onClick={() => setPage(page - 1)}>
              ← 新一些
            </button>
            <span className="field__hint">
              第 {page + 1} / {Math.ceil(total / SIZE)} 页
            </span>
            <button
              className="chatlog__more"
              disabled={(page + 1) * SIZE >= total}
              onClick={() => setPage(page + 1)}
            >
              旧一些 →
            </button>
          </div>
        )}
      </section>
    </>
  );
}
