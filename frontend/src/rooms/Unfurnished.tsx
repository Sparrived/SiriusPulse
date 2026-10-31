import { motion } from 'motion/react';
import type { Room } from '@/rooms';
import './unfurnished.css';

/** 还没搬进新家的房间：如实说明，并把旧版里对应的功能递到手边。 */
export function Unfurnished({ room }: { room: Room }) {
  return (
    <motion.section
      className="unfurnished paper"
      initial={{ opacity: 0, y: 30, rotate: -0.8 }}
      animate={{ opacity: 1, y: 0, rotate: 0 }}
      exit={{ opacity: 0, y: 16 }}
      transition={{ type: 'spring', stiffness: 120, damping: 18 }}
    >
      <room.Icon width={44} height={44} className="unfurnished__icon" aria-hidden />
      <h1 className="hand">{room.name}</h1>
      <p className="unfurnished__hint">{room.hint}</p>
      <p className="unfurnished__note hand">这间屋子还在布置中，纸箱还没拆完呢。</p>
      {room.legacy && (
        <>
          <p className="unfurnished__lead">搬好之前，这些事先在旧版控制台里做：</p>
          <div className="unfurnished__links">
            {room.legacy.map((l) => (
              <a
                key={l.page}
                className="soft-btn ghost"
                href={`/legacy/#${l.page}`}
                target="_blank"
                rel="noreferrer"
              >
                {l.label} ↗
              </a>
            ))}
          </div>
        </>
      )}
    </motion.section>
  );
}
