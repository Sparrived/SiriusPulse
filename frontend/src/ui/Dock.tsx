import { motion } from 'motion/react';
import { useLocation } from 'wouter';
import { ROOMS } from '@/rooms';
import './dock.css';

/** 底部的小码头：每一格是家里的一个房间。没有侧边栏，也没有层级菜单。 */
export function Dock() {
  const [location, navigate] = useLocation();
  return (
    <nav className="dock paper" aria-label="房间">
      {ROOMS.map((room) => {
        const here = location === room.path;
        return (
          <button
            key={room.id}
            className={`dock__item${here ? ' is-here' : ''}`}
            onClick={() => navigate(room.path)}
            aria-current={here ? 'page' : undefined}
            title={room.hint}
          >
            {here && (
              <motion.span
                layoutId="dock-here"
                className="dock__glow"
                transition={{ type: 'spring', stiffness: 380, damping: 32 }}
              />
            )}
            <room.Icon className="dock__icon" aria-hidden />
            <span className="dock__label hand">{room.name}</span>
          </button>
        );
      })}
    </nav>
  );
}
