import { AnimatePresence, motion } from 'motion/react';
import { useState } from 'react';
import { Markdown } from '@/lib/markdown';
import { useLife, useWork } from '@/lib/queries-journal';
import type { LifeProject } from '@/lib/types-journal';
import { ago } from '@/lib/time';

const STATUS: Record<string, string> = {
  active: '在做',
  paused: '先放着',
  done: '做完了',
  dropped: '放下了',
};

function ProjectCard({
  project,
  chars,
  onOpen,
}: {
  project: LifeProject;
  chars?: number;
  onOpen?: () => void;
}) {
  const last = project.steps[project.steps.length - 1];
  return (
    <motion.article
      className={`works__card paper is-${project.status}`}
      initial={{ opacity: 0, y: 12 }}
      animate={{ opacity: 1, y: 0 }}
    >
      <header>
        <span className="chip">{project.kind_label}</span>
        <span className={`works__status is-${project.status}`}>
          {STATUS[project.status] ?? project.status}
        </span>
      </header>
      <h3 className="hand">{project.title}</h3>
      {project.why && <p className="works__why">{project.why}</p>}
      {last && (
        <p className="works__step">
          上次：{last.did} <em>{ago(last.at)}</em>
        </p>
      )}
      {project.status === 'active' && project.next_step && (
        <p className="works__step">下一步：{project.next_step}</p>
      )}
      {project.outcome && project.status !== 'active' && <p className="works__step">{project.outcome}</p>}
      {onOpen ? (
        <button className="soft-btn ghost works__open" onClick={onOpen}>
          翻开作品{chars ? ` · ${chars.toLocaleString('zh-CN')} 字` : ''}
        </button>
      ) : (
        <p className="field__hint">还没有写下东西。</p>
      )}
    </motion.article>
  );
}

function Reader({ projectId, onClose }: { projectId: string; onClose: () => void }) {
  const { data, isLoading } = useWork(projectId);
  return (
    <motion.section
      className="works__reader sheet paper"
      initial={{ opacity: 0, y: 20 }}
      animate={{ opacity: 1, y: 0 }}
      exit={{ opacity: 0, y: 10 }}
    >
      <button className="diary__today works__back" onClick={onClose}>
        ← 合上
      </button>
      {isLoading ? (
        <p className="mirror__loading hand">翻开……</p>
      ) : data?.markdown ? (
        <div className="works__doc">
          <Markdown source={data.markdown} />
        </div>
      ) : (
        <p className="hand">这里还是空白的。</p>
      )}
    </motion.section>
  );
}

/** 她的作品：她给自己找的事，以及一点点写下来的东西。 */
export function Works() {
  const { data, isLoading } = useLife();
  const [open, setOpen] = useState<string | null>(null);
  if (isLoading) return <p className="mirror__loading hand">翻她的本子……</p>;

  const projects = data?.projects ?? [];
  const chars = new Map((data?.works ?? []).map((w) => [w.project_id, w.chars]));

  if (!projects.length) {
    return (
      <section className="sheet paper daylife__empty">
        <p className="hand">她还没有给自己找事做。</p>
        <p className="field__hint">
          她排日程时会从自己的兴趣和惦记的事里挑一件开始：读一本书、学一样东西、写点什么……做出来的东西都会收在这里。
        </p>
      </section>
    );
  }

  return (
    <AnimatePresence mode="wait">
      {open ? (
        <Reader key="reader" projectId={open} onClose={() => setOpen(null)} />
      ) : (
        <motion.div
          key="list"
          className="works"
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
        >
          {projects.map((project) => (
            <ProjectCard
              key={project.project_id}
              project={project}
              chars={chars.get(project.project_id)}
              onOpen={chars.has(project.project_id) ? () => setOpen(project.project_id) : undefined}
            />
          ))}
        </motion.div>
      )}
    </AnimatePresence>
  );
}
