import { motion } from 'motion/react';
import { useEffect, type ReactNode } from 'react';
import { createPortal } from 'react-dom';
import './drawer.css';

/**
 * 从右边滑出的一页纸。挂在 body 上：房间那一列有位移动画，fixed 定位会被它困住。
 * 外层请用 AnimatePresence 包住，退场才有动画。
 */
export function Drawer({
  title,
  kicker,
  onClose,
  children,
  footer,
  wide,
}: {
  title: ReactNode;
  kicker?: ReactNode;
  onClose: () => void;
  children: ReactNode;
  footer?: ReactNode;
  wide?: boolean;
}) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && onClose();
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  return createPortal(
    <motion.div
      className="drawer-veil"
      initial={{ opacity: 0 }}
      animate={{ opacity: 1 }}
      exit={{ opacity: 0 }}
      onClick={onClose}
    >
      <motion.aside
        className={`drawer paper${wide ? ' drawer--wide' : ''}`}
        role="dialog"
        onClick={(e) => e.stopPropagation()}
        initial={{ x: 70, opacity: 0 }}
        animate={{ x: 0, opacity: 1 }}
        exit={{ x: 50, opacity: 0 }}
        transition={{ type: 'spring', stiffness: 220, damping: 26 }}
      >
        <header className="drawer__head">
          <div>
            {kicker && <p className="drawer__kicker">{kicker}</p>}
            <h2 className="hand">{title}</h2>
          </div>
          <button className="star-card__close" onClick={onClose} aria-label="关上">
            ×
          </button>
        </header>
        <div className="drawer__body">{children}</div>
        {footer && <footer className="drawer__foot">{footer}</footer>}
      </motion.aside>
    </motion.div>,
    document.body,
  );
}
