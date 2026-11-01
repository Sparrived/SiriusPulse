import { motion } from 'motion/react';
import { useRef, useState } from 'react';
import { useAvatar, useAvatarChange } from '@/lib/queries';
import { useLive } from '@/state/live';
import { Sheet } from '@/ui/form/fields';

const MAX_BYTES = 8 * 1024 * 1024;

export function AvatarTab({ persona, name }: { persona: string; name: string }) {
  const { data: avatar } = useAvatar(persona);
  const change = useAvatarChange(persona);
  const input = useRef<HTMLInputElement>(null);
  const [over, setOver] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);

  const take = (file: File | undefined) => {
    if (!file) return;
    if (!file.type.startsWith('image/')) return setProblem('这好像不是图片呢');
    if (file.size > MAX_BYTES) return setProblem('图片太大啦，8 MB 以内就好');
    setProblem(null);
    change.mutate(file, {
      onSuccess: () => useLive.getState().think('feel', '换上新衣服啦，好看吗？'),
      onError: (e) => setProblem((e as Error).message),
    });
  };

  return (
    <Sheet title={`${name}的样子`}>
      <div
        className={`avatar-drop${over ? ' is-over' : ''}`}
        onDragOver={(e) => {
          e.preventDefault();
          setOver(true);
        }}
        onDragLeave={() => setOver(false)}
        onDrop={(e) => {
          e.preventDefault();
          setOver(false);
          take(e.dataTransfer.files[0]);
        }}
        onClick={() => input.current?.click()}
        role="button"
        tabIndex={0}
        onKeyDown={(e) => (e.key === 'Enter' || e.key === ' ') && input.current?.click()}
      >
        {avatar && (
          <motion.img
            key={avatar.url}
            src={avatar.url}
            alt={`${name}的立绘`}
            initial={{ opacity: 0, scale: 0.94 }}
            animate={{ opacity: change.isPending ? 0.4 : 1, scale: 1 }}
          />
        )}
        <span className="avatar-drop__hint hand">
          {change.isPending ? '正在换衣服……' : over ? '松手就换上' : '把新立绘拖到这里，或点一下选图'}
        </span>
      </div>
      <input
        ref={input}
        type="file"
        accept="image/png,image/webp,image/jpeg,image/gif"
        hidden
        onChange={(e) => {
          take(e.target.files?.[0]);
          e.target.value = '';
        }}
      />
      <p className="field__hint">
        最好是背景透明的 PNG：半身或全身、人物居中、头顶留一点空。下沿会自然融进云里。会被缩到最长边 2048
        像素。
      </p>
      {problem && <p className="mirror__error">{problem}</p>}
      {avatar?.custom && (
        <button
          type="button"
          className="soft-btn ghost avatar-reset"
          disabled={change.isPending}
          onClick={() => change.mutate(null)}
        >
          换回默认的样子
        </button>
      )}
    </Sheet>
  );
}
