import { AnimatePresence, motion } from 'motion/react';
import { createContext, useContext, useEffect, useMemo, useState, type ReactNode } from 'react';
import { Chips, Segmented, Toggle } from '@/ui/form/fields';
import { isSecretField, type UIPresentation, type UISchema } from './schema';
import './params.css';

/** 插件/小工具声明的参数（与后端 _masked_parameter / TOOL_META 的形状一致） */
export interface ParamDef {
  name: string;
  type?: string;
  description?: string;
  required?: boolean;
  default?: unknown;
  choices?: unknown[] | null;
  fields?: ParamDef[] | null;
  minimum?: number | null;
  maximum?: number | null;
  group?: string;
  /** 插件作者显式允许在 WebUI 里保存这个凭据 */
  persist_secret?: boolean;
}

export type Settings = Record<string, unknown>;

/** 服务端把已保存的凭据回显成掩码；原样提交表示「不改」 */
export const SECRET_MASK = '********';

const kind = (p: ParamDef) => String(p.type ?? 'str').toLowerCase();
const clone = <T,>(v: T): T => (v === undefined ? v : JSON.parse(JSON.stringify(v)));

function emptyFor(p: ParamDef): unknown {
  const t = kind(p);
  if (p.default !== undefined && p.default !== null) return clone(p.default);
  if (['list', 'array', 'checkbox_group', 'schedule', 'object_array'].includes(t)) return [];
  if (['object', 'json'].includes(t)) return {};
  if (['bool', 'boolean'].includes(t)) return false;
  if (['str', 'string', 'model', 'password', 'secret'].includes(t)) return '';
  return null;
}

/** 表单的初始值：已保存的设置优先，缺的参数补上声明里的默认值 */
export function withDefaults(params: ParamDef[], settings: Settings | undefined): Settings {
  const out: Settings = {};
  for (const p of params)
    out[p.name] = settings && p.name in settings ? clone(settings[p.name]) : emptyFor(p);
  return out;
}

export function newObjectItem(fields: ParamDef[]): Settings {
  return withDefaults(fields, undefined);
}

// JSON 字段写坏了时登记在这里，外层据此禁止保存
const JsonErrors = createContext<(key: string, bad: boolean) => void>(() => {});

export function useJsonErrors() {
  const [bad, setBad] = useState<Set<string>>(new Set());
  const report = useMemo(
    () => (key: string, isBad: boolean) =>
      setBad((prev) => {
        if (prev.has(key) === isBad) return prev;
        const next = new Set(prev);
        if (isBad) next.add(key);
        else next.delete(key);
        return next;
      }),
    [],
  );
  return { hasErrors: bad.size > 0, report };
}

/**
 * editable：凭据可以直接填（小工具）。
 * declared：只有声明了 persist_secret 的凭据能填，其余必须走环境变量（插件；后端会拒收明文）。
 */
export type SecretPolicy = 'editable' | 'declared';

interface Ctx {
  models: { label: string; value: string }[];
  secretPolicy: SecretPolicy;
}
const FormCtx = createContext<Ctx>({ models: [], secretPolicy: 'editable' });

// ── 单个字段 ────────────────────────────────────────────

function NumberField({
  p,
  value,
  onChange,
  unit,
}: {
  p: ParamDef;
  value: unknown;
  onChange: (v: unknown) => void;
  unit?: string;
}) {
  const isInt = kind(p) === 'int';
  const [text, setText] = useState<string | null>(null);
  const shown = text ?? (value === null || value === undefined ? '' : String(value));
  const bounds = [p.minimum != null ? `≥ ${p.minimum}` : '', p.maximum != null ? `≤ ${p.maximum}` : '']
    .filter(Boolean)
    .join('，');
  return (
    <span className="pf-number">
      <input
        className="input num"
        inputMode={isInt ? 'numeric' : 'decimal'}
        value={shown}
        placeholder={bounds}
        onChange={(e) => {
          setText(e.target.value);
          const raw = e.target.value.trim();
          if (!raw) return onChange(null);
          const n = Number(raw);
          if (Number.isFinite(n)) onChange(isInt ? Math.trunc(n) : n);
        }}
        onBlur={() => setText(null)}
      />
      {unit && <em>{unit}</em>}
    </span>
  );
}

function SecretField({ value, onChange }: { value: unknown; onChange: (v: unknown) => void }) {
  const saved = value === SECRET_MASK;
  const [peek, setPeek] = useState(false);
  return (
    <span className="input-wrap">
      <input
        className="input input--mono"
        type={peek ? 'text' : 'password'}
        value={saved ? '' : String(value ?? '')}
        placeholder={saved ? '已保存（留空就不改）' : ''}
        autoComplete="new-password"
        onChange={(e) => onChange(e.target.value === '' && saved ? SECRET_MASK : e.target.value)}
      />
      {!saved && (
        <button type="button" className="input__peek" onClick={() => setPeek((v) => !v)}>
          {peek ? '藏起来' : '看一眼'}
        </button>
      )}
    </span>
  );
}

function JsonField({ id, value, onChange }: { id: string; value: unknown; onChange: (v: unknown) => void }) {
  const report = useContext(JsonErrors);
  const [text, setText] = useState(() => JSON.stringify(value ?? {}, null, 2));
  const [error, setError] = useState<string | null>(null);
  useEffect(() => () => report(id, false), [id, report]);
  return (
    <>
      <textarea
        className="input input--mono pf-json"
        rows={Math.min(14, Math.max(4, text.split('\n').length))}
        value={text}
        spellCheck={false}
        onChange={(e) => {
          setText(e.target.value);
          try {
            const parsed = JSON.parse(e.target.value || '{}');
            if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed))
              throw new Error('需要一个 JSON 对象');
            setError(null);
            report(id, false);
            onChange(parsed);
          } catch (err) {
            setError((err as Error).message);
            report(id, true);
          }
        }}
      />
      {error && <span className="chips__error">JSON 有误：{error}</span>}
    </>
  );
}

function ScheduleField({ value, onChange }: { value: unknown; onChange: (v: unknown) => void }) {
  const rows = Array.isArray(value) ? (value as { time: string; duration: number }[]) : [];
  const set = (i: number, patch: Partial<{ time: string; duration: number }>) =>
    onChange(rows.map((r, j) => (j === i ? { ...r, ...patch } : r)));
  return (
    <div className="pf-schedule">
      {rows.map((r, i) => (
        <span key={i} className="pf-schedule__row">
          <input
            className="input num"
            type="time"
            value={r.time ?? ''}
            onChange={(e) => set(i, { time: e.target.value })}
          />
          <span>持续</span>
          <input
            className="input num"
            inputMode="numeric"
            value={r.duration ?? ''}
            onChange={(e) => set(i, { duration: Math.trunc(Number(e.target.value) || 0) })}
          />
          <span>分钟</span>
          <button
            type="button"
            className="groups__remove"
            onClick={() => onChange(rows.filter((_, j) => j !== i))}
          >
            ×
          </button>
        </span>
      ))}
      <button
        type="button"
        className="pf-add"
        onClick={() => onChange([...rows, { time: '09:00', duration: 60 }])}
      >
        ＋ 加一个时段
      </button>
    </div>
  );
}

function Control({
  p,
  pres,
  value,
  onChange,
  path,
}: {
  p: ParamDef;
  pres?: UIPresentation;
  value: unknown;
  onChange: (v: unknown) => void;
  path: string;
}) {
  const { models, secretPolicy } = useContext(FormCtx);
  const t = kind(p);
  const choices = Array.isArray(p.choices) ? p.choices.map(String) : null;

  if (t === 'password' || t === 'secret' || isSecretField(p.name, t)) {
    if (secretPolicy === 'declared' && !p.persist_secret) {
      // 值原样留在表单里（服务端只会回显空或掩码），不给一个注定被拒的输入框
      return <p className="pf-secret-note">由环境变量或 Secret 管理器提供，不能在这里保存。</p>;
    }
    if (['str', 'string', 'password', 'secret'].includes(t))
      return <SecretField value={value} onChange={onChange} />;
  }
  if (t === 'bool' || t === 'boolean') {
    return (
      <Toggle
        on={value === true}
        onChange={onChange}
        label={value === true ? (pres?.true_label ?? '开') : (pres?.false_label ?? '关')}
      />
    );
  }
  if (t === 'int' || t === 'float' || t === 'number')
    return <NumberField p={p} value={value} onChange={onChange} unit={pres?.unit} />;
  if (t === 'model') {
    const v = String(value ?? '');
    return (
      <select className="input" value={v} onChange={(e) => onChange(e.target.value)}>
        <option value="">（沿用默认）</option>
        {!models.some((m) => m.value === v) && v && <option value={v}>{v}</option>}
        {models.map((m) => (
          <option key={m.value} value={m.value}>
            {m.label}
          </option>
        ))}
      </select>
    );
  }
  if (t === 'checkbox_group') {
    const list = Array.isArray(value) ? (value as string[]) : [];
    return (
      <span className="pf-checks">
        {(choices ?? []).map((c) => (
          <button
            key={c}
            type="button"
            className={`pf-check${list.includes(c) ? ' is-on' : ''}`}
            onClick={() => onChange(list.includes(c) ? list.filter((x) => x !== c) : [...list, c])}
          >
            {c}
          </button>
        ))}
      </span>
    );
  }
  if (t === 'list' || t === 'array') {
    return (
      <Chips
        values={Array.isArray(value) ? (value as unknown[]).map(String) : []}
        onChange={onChange}
        placeholder={pres?.item_placeholder ?? pres?.add_label ?? '回车添加'}
      />
    );
  }
  if (t === 'schedule') return <ScheduleField value={value} onChange={onChange} />;
  if (t === 'object' || t === 'json') return <JsonField id={path} value={value} onChange={onChange} />;
  if (t === 'object_array')
    return <ObjectArray p={p} pres={pres} value={value} onChange={onChange} path={path} />;

  // 字符串：有可选值就做成分段/下拉，否则按 widget 选输入框
  const v = String(value ?? '');
  if (choices?.length) {
    return choices.length <= 4 ? (
      <Segmented
        size="sm"
        value={v}
        options={choices.map((c) => ({ value: c, label: c }))}
        onChange={onChange}
      />
    ) : (
      <select className="input" value={v} onChange={(e) => onChange(e.target.value)}>
        {!choices.includes(v) && <option value={v}>{v || '（未选择）'}</option>}
        {choices.map((c) => (
          <option key={c}>{c}</option>
        ))}
      </select>
    );
  }
  if (pres?.widget === 'textarea') {
    return (
      <textarea
        className="input"
        rows={4}
        value={v}
        placeholder={pres.placeholder}
        onChange={(e) => onChange(e.target.value)}
      />
    );
  }
  return (
    <input
      className={`input${pres?.widget === 'code' || pres?.widget === 'path' || pres?.widget === 'url' ? ' input--mono' : ''}`}
      value={v}
      placeholder={pres?.placeholder}
      inputMode={pres?.widget === 'url' ? 'url' : undefined}
      spellCheck={false}
      onChange={(e) => onChange(e.target.value)}
    />
  );
}

function FieldShell({
  p,
  pres,
  children,
  wide,
}: {
  p: ParamDef;
  pres?: UIPresentation;
  children: ReactNode;
  wide?: boolean;
}) {
  const span =
    pres?.span ?? (kind(p) === 'object_array' || kind(p) === 'object' || kind(p) === 'json' ? 12 : 6);
  return (
    <div className="pf-field" style={{ gridColumn: `span ${wide ? 12 : span}` }}>
      <span className="pf-label">
        <span className="hand">{pres?.label ?? p.name}</span>
        {p.required && <i title="必填">*</i>}
        {!pres?.label && <code>{kind(p)}</code>}
      </span>
      {children}
      {(pres?.help ?? p.description) && <span className="field__hint">{pres?.help ?? p.description}</span>}
    </div>
  );
}

// ── 对象列表：每一项一张小卡片 ─────────────────────────

function ObjectArray({
  p,
  pres,
  value,
  onChange,
  path,
}: {
  p: ParamDef;
  pres?: UIPresentation;
  value: unknown;
  onChange: (v: unknown) => void;
  path: string;
}) {
  const fields = p.fields ?? [];
  const rows = Array.isArray(value) ? (value as Settings[]) : [];
  const [open, setOpen] = useState<number | null>(rows.length === 1 ? 0 : null);
  const fieldPres = pres?.fields ?? {};

  if (!fields.length)
    return (
      <JsonField id={path} value={{ items: rows }} onChange={(v) => onChange((v as Settings).items ?? [])} />
    );

  const title = (row: Settings, i: number) =>
    String(
      (pres?.item_title_field && row[pres.item_title_field]) ||
        (pres?.item_fallback_field && row[pres.item_fallback_field]) ||
        pres?.item_placeholder ||
        `第 ${i + 1} 项`,
    );
  const grouped = pres?.fieldsets?.length
    ? [
        ...pres.fieldsets.map((fs) => ({
          ...fs,
          defs: fs.fields.map((n) => fields.find((f) => f.name === n)!).filter(Boolean),
        })),
        {
          id: '_rest',
          title: undefined,
          description: undefined,
          collapsed: false,
          fields: [],
          defs: fields.filter((f) => !pres.fieldsets!.some((fs) => fs.fields.includes(f.name))),
        },
      ]
    : [{ id: '_all', title: undefined, description: undefined, collapsed: false, fields: [], defs: fields }];

  return (
    <div className="pf-objects">
      {!rows.length && (
        <div className="pf-empty">
          <p className="hand">{pres?.empty_title ?? '还是空的'}</p>
          {pres?.empty_description && <p className="field__hint">{pres.empty_description}</p>}
        </div>
      )}
      {rows.map((row, i) => {
        const status = pres?.item_status_field ? row[pres.item_status_field] : undefined;
        return (
          <div key={i} className={`pf-object${open === i ? ' is-open' : ''}`}>
            <button type="button" className="pf-object__head" onClick={() => setOpen(open === i ? null : i)}>
              {status !== undefined && <i className={`pf-dot${status ? ' is-on' : ''}`} />}
              <span className="hand">{title(row, i)}</span>
              {pres?.item_subtitle_field && row[pres.item_subtitle_field] != null && (
                <em>{String(row[pres.item_subtitle_field])}</em>
              )}
              {pres?.item_badge_field && row[pres.item_badge_field] != null && (
                <code>{String(row[pres.item_badge_field])}</code>
              )}
              <span className="pf-object__caret">{open === i ? '收起' : '展开'}</span>
            </button>
            <AnimatePresence initial={false}>
              {open === i && (
                <motion.div
                  className="pf-object__body"
                  initial={{ height: 0, opacity: 0 }}
                  animate={{ height: 'auto', opacity: 1 }}
                  exit={{ height: 0, opacity: 0 }}
                >
                  {grouped
                    .filter((g) => g.defs.length)
                    .map((g) => (
                      <fieldset key={g.id} className="pf-fieldset">
                        {g.title && <legend className="hand">{g.title}</legend>}
                        {g.description && <p className="field__hint">{g.description}</p>}
                        <div className="pf-grid">
                          {g.defs.map((f) => (
                            <FieldShell key={f.name} p={f} pres={fieldPres[f.name]}>
                              <Control
                                p={f}
                                pres={fieldPres[f.name]}
                                path={`${path}[${i}].${f.name}`}
                                value={row[f.name]}
                                onChange={(v) =>
                                  onChange(rows.map((r, j) => (j === i ? { ...r, [f.name]: v } : r)))
                                }
                              />
                            </FieldShell>
                          ))}
                        </div>
                      </fieldset>
                    ))}
                  <button
                    type="button"
                    className="star-card__farewell pf-object__remove"
                    onClick={() => {
                      onChange(rows.filter((_, j) => j !== i));
                      setOpen(null);
                    }}
                  >
                    删掉这一项
                  </button>
                </motion.div>
              )}
            </AnimatePresence>
          </div>
        );
      })}
      <button
        type="button"
        className="pf-add"
        onClick={() => {
          onChange([...rows, newObjectItem(fields)]);
          setOpen(rows.length);
        }}
      >
        ＋ {pres?.add_label ?? '加一项'}
      </button>
    </div>
  );
}

// ── 整张表单 ────────────────────────────────────────────

/**
 * 按参数声明画表单。有合法的 ui_schema 就按它分区排版；没有（或校验不通过）就按参数的 group 分组。
 * 值始终是完整的设置对象：保存时整份提交，凭据字段保持掩码即表示不改。
 */
export function ParamForm({
  params,
  schema,
  value,
  onChange,
  models = [],
  secretPolicy = 'editable',
  onJsonErrors,
}: {
  params: ParamDef[];
  schema?: UISchema | null;
  value: Settings;
  onChange: (v: Settings) => void;
  models?: { label: string; value: string }[];
  secretPolicy?: SecretPolicy;
  onJsonErrors: (key: string, bad: boolean) => void;
}) {
  const byName = new Map(params.map((p) => [p.name, p]));
  const pres = schema?.parameters ?? {};
  const set = (name: string) => (v: unknown) => onChange({ ...value, [name]: v });

  const sections = schema?.sections?.length
    ? [
        ...schema.sections.map((s) => ({
          ...s,
          defs: s.parameters.map((n) => byName.get(n)!).filter(Boolean),
        })),
        {
          id: '_rest',
          title: '其它',
          description: undefined,
          columns: 2 as const,
          collapsed: false,
          tone: 'default' as const,
          parameters: [],
          defs: params.filter((p) => !schema.sections!.some((s) => s.parameters.includes(p.name))),
        },
      ]
    : Object.entries(
        params.reduce<Record<string, ParamDef[]>>((acc, p) => {
          (acc[p.group || '设置'] ??= []).push(p);
          return acc;
        }, {}),
      ).map(([group, defs]) => ({
        id: group,
        title: group,
        description: undefined,
        columns: 2 as const,
        collapsed: false,
        tone: 'default' as const,
        parameters: [],
        defs,
      }));

  return (
    <FormCtx.Provider value={{ models, secretPolicy }}>
      <JsonErrors.Provider value={onJsonErrors}>
        <div className="pf">
          {schema?.description && <p className="field__hint pf-intro">{schema.description}</p>}
          {sections
            .filter((s) => s.defs.length)
            .map((s) => (
              <Section
                key={s.id}
                title={s.title}
                description={s.description}
                collapsed={s.collapsed}
                tone={s.tone}
              >
                <div className={`pf-grid${s.columns === 1 ? ' pf-grid--one' : ''}`}>
                  {s.defs.map((p) => (
                    <FieldShell key={p.name} p={p} pres={pres[p.name]} wide={s.columns === 1}>
                      <Control
                        p={p}
                        pres={pres[p.name]}
                        path={p.name}
                        value={value[p.name]}
                        onChange={set(p.name)}
                      />
                    </FieldShell>
                  ))}
                </div>
              </Section>
            ))}
        </div>
      </JsonErrors.Provider>
    </FormCtx.Provider>
  );
}

function Section({
  title,
  description,
  collapsed,
  tone,
  children,
}: {
  title?: string;
  description?: string;
  collapsed?: boolean;
  tone?: string;
  children: ReactNode;
}) {
  const [open, setOpen] = useState(!collapsed);
  return (
    <section className={`pf-section pf-section--${tone ?? 'default'}`}>
      {title && (
        <button
          type="button"
          className="pf-section__head"
          onClick={() => setOpen((v) => !v)}
          aria-expanded={open}
        >
          <span className="hand">{title}</span>
          <em>{open ? '收起' : '展开'}</em>
        </button>
      )}
      {description && open && <p className="field__hint">{description}</p>}
      {open && children}
    </section>
  );
}
