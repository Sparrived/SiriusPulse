import { AnimatePresence, motion } from 'motion/react';
import { useId, useState, type ReactNode } from 'react';
import type { SaveStatus } from './useDraft';
import './form.css';

export function Field({
  label,
  hint,
  children,
  inline,
}: {
  label: ReactNode;
  hint?: ReactNode;
  children: ReactNode;
  inline?: boolean;
}) {
  // 不用 <label> 包裹：里面若有按钮（如 Stepper 的 −），点标题文字会误触第一个按钮
  return (
    <div className={`field${inline ? ' field--inline' : ''}`} role="group">
      <span className="field__label hand">{label}</span>
      {children}
      {hint && <span className="field__hint">{hint}</span>}
    </div>
  );
}

export function TextInput({
  value,
  onChange,
  placeholder,
  secret,
  mono,
  ...rest
}: {
  value: string;
  onChange: (v: string) => void;
  placeholder?: string;
  secret?: boolean;
  mono?: boolean;
  readOnly?: boolean;
  inputMode?: 'numeric' | 'text' | 'url';
}) {
  const [peek, setPeek] = useState(false);
  return (
    <span className="input-wrap">
      <input
        className={`input${mono ? ' input--mono' : ''}`}
        type={secret && !peek ? 'password' : 'text'}
        value={value}
        placeholder={placeholder}
        onChange={(e) => onChange(e.target.value)}
        spellCheck={false}
        autoComplete="off"
        {...rest}
      />
      {secret && (
        <button
          type="button"
          className="input__peek"
          onClick={(e) => {
            e.preventDefault();
            setPeek((v) => !v);
          }}
        >
          {peek ? '藏起来' : '看一眼'}
        </button>
      )}
    </span>
  );
}

/** 写信用的纸：带横线、随内容长高 */
export function Letter({
  value,
  onChange,
  placeholder,
  minRows = 10,
}: {
  value: string;
  onChange: (v: string) => void;
  placeholder?: string;
  minRows?: number;
}) {
  return (
    <textarea
      className="letter hand"
      value={value}
      placeholder={placeholder}
      rows={Math.max(minRows, value.split('\n').length + 1)}
      onChange={(e) => onChange(e.target.value)}
    />
  );
}

/** 小标签：别名、群号、名字这类「一串东西」 */
export function Chips({
  values,
  onChange,
  placeholder,
  validate,
  render,
}: {
  values: string[];
  onChange: (v: string[]) => void;
  placeholder?: string;
  /** 返回错误文案表示不收 */
  validate?: (v: string) => string | null;
  render?: (v: string) => ReactNode;
}) {
  const [text, setText] = useState('');
  const [error, setError] = useState<string | null>(null);
  const add = () => {
    const parts = text
      .split(/[\s,，、]+/)
      .map((s) => s.trim())
      .filter(Boolean);
    if (!parts.length) return;
    for (const p of parts) {
      const problem = validate?.(p);
      if (problem) {
        setError(problem);
        return;
      }
    }
    onChange([...values, ...parts.filter((p) => !values.includes(p))]);
    setText('');
    setError(null);
  };
  return (
    <div className="chips">
      <AnimatePresence initial={false}>
        {values.map((v) => (
          <motion.span
            key={v}
            className="chip-tag"
            layout
            initial={{ opacity: 0, scale: 0.6 }}
            animate={{ opacity: 1, scale: 1 }}
            exit={{ opacity: 0, scale: 0.6 }}
          >
            {render ? render(v) : v}
            <button
              type="button"
              aria-label={`移除 ${v}`}
              onClick={() => onChange(values.filter((x) => x !== v))}
            >
              ×
            </button>
          </motion.span>
        ))}
      </AnimatePresence>
      <input
        className="chips__input"
        value={text}
        placeholder={placeholder}
        onChange={(e) => {
          setText(e.target.value);
          setError(null);
        }}
        onKeyDown={(e) => {
          if (e.key === 'Enter' || e.key === ',' || e.key === '，') {
            e.preventDefault();
            add();
          } else if (e.key === 'Backspace' && !text && values.length) {
            onChange(values.slice(0, -1));
          }
        }}
        onBlur={add}
      />
      {error && <span className="chips__error">{error}</span>}
    </div>
  );
}

export function Toggle({
  on,
  onChange,
  label,
  sub,
}: {
  on: boolean;
  onChange: (v: boolean) => void;
  label: ReactNode;
  sub?: ReactNode;
}) {
  const id = useId();
  return (
    <div className="toggle-row">
      <span>
        <span className="toggle-row__label hand" id={id}>
          {label}
        </span>
        {sub && <span className="field__hint">{sub}</span>}
      </span>
      <button
        type="button"
        role="switch"
        aria-checked={on}
        aria-labelledby={id}
        className={`toggle${on ? ' is-on' : ''}`}
        onClick={() => onChange(!on)}
      >
        <motion.span
          className="toggle__knob"
          layout
          transition={{ type: 'spring', stiffness: 600, damping: 32 }}
        />
      </button>
    </div>
  );
}

/** 带单位的数字：左右小按钮 + 可直接输入 */
export function Stepper({
  value,
  onChange,
  min = 0,
  max = Number.MAX_SAFE_INTEGER,
  step = 1,
  unit,
  decimals = 0,
}: {
  value: number;
  onChange: (v: number) => void;
  min?: number;
  max?: number;
  step?: number;
  unit?: string;
  decimals?: number;
}) {
  const clamp = (v: number) => Math.min(max, Math.max(min, Number(v.toFixed(decimals))));
  const [text, setText] = useState<string | null>(null);
  return (
    <span className="stepper">
      <button type="button" onClick={() => onChange(clamp(value - step))} aria-label="减少">
        −
      </button>
      <input
        className="num"
        inputMode="decimal"
        value={text ?? String(value)}
        onChange={(e) => setText(e.target.value)}
        onBlur={() => {
          const n = Number(text);
          if (text !== null && Number.isFinite(n)) onChange(clamp(n));
          setText(null);
        }}
        onKeyDown={(e) => e.key === 'Enter' && (e.target as HTMLInputElement).blur()}
      />
      {unit && <span className="stepper__unit">{unit}</span>}
      <button type="button" onClick={() => onChange(clamp(value + step))} aria-label="增加">
        +
      </button>
    </span>
  );
}

export function Segmented<T extends string>({
  value,
  options,
  onChange,
  size = 'md',
}: {
  value: T;
  options: { value: T; label: ReactNode }[];
  onChange: (v: T) => void;
  size?: 'sm' | 'md';
}) {
  const group = useId();
  return (
    <div className={`segmented segmented--${size}`} role="tablist">
      {options.map((o) => (
        <button
          key={o.value}
          type="button"
          role="tab"
          aria-selected={o.value === value}
          className={o.value === value ? 'is-on' : ''}
          onClick={() => onChange(o.value)}
        >
          {o.value === value && (
            <motion.span
              layoutId={`seg-${group}`}
              className="segmented__pill"
              transition={{ type: 'spring', stiffness: 420, damping: 34 }}
            />
          )}
          <span className="segmented__text">{o.label}</span>
        </button>
      ))}
    </div>
  );
}

const STATUS_TEXT: Record<SaveStatus, string> = {
  idle: '',
  pending: '正在写……',
  saving: '正在记下……',
  saved: '记下啦',
  error: '没记住',
};

/** 自动保存的小声提示，代替「保存」按钮和弹窗 */
export function SaveWhisper({
  status,
  error,
  retry,
}: {
  status: SaveStatus;
  error?: string | null;
  retry?: () => void;
}) {
  return (
    <span className={`whisper whisper--${status}`} aria-live="polite">
      <AnimatePresence mode="wait">
        {status !== 'idle' && (
          <motion.span
            key={status}
            initial={{ opacity: 0, y: 4 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: -4 }}
            className="hand"
          >
            {STATUS_TEXT[status]}
            {status === 'error' && (
              <>
                {error ? `：${error}` : ''}
                {retry && (
                  <button type="button" onClick={retry}>
                    再试一次
                  </button>
                )}
              </>
            )}
          </motion.span>
        )}
      </AnimatePresence>
    </span>
  );
}

/** 房间里的一张纸：标题 + 内容；可折叠 */
export function Sheet({
  title,
  aside,
  children,
  fold,
  className,
}: {
  title: ReactNode;
  aside?: ReactNode;
  children: ReactNode;
  fold?: boolean;
  className?: string;
}) {
  const [open, setOpen] = useState(!fold);
  return (
    <section className={`sheet paper${open ? '' : ' is-folded'} ${className ?? ''}`}>
      <header className="sheet__head">
        <h2 className="hand">{title}</h2>
        {aside}
        {fold && (
          <button
            type="button"
            className="sheet__fold"
            onClick={() => setOpen((v) => !v)}
            aria-expanded={open}
          >
            {open ? '收起' : '展开'}
          </button>
        )}
      </header>
      <AnimatePresence initial={false}>
        {open && (
          <motion.div
            className="sheet__body"
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: 'auto', opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            transition={{ duration: 0.35, ease: [0.22, 1, 0.36, 1] }}
          >
            <div className="sheet__inner">{children}</div>
          </motion.div>
        )}
      </AnimatePresence>
    </section>
  );
}
