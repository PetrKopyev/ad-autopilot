import type { ButtonHTMLAttributes } from 'react';
import { Icon, type IconName } from './Icon';

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  /** primary — тёмная, accent — главное действие экрана («Запустить рекламу»), danger — необратимое. */
  variant?: 'primary' | 'secondary' | 'danger' | 'accent' | 'ghost';
  size?: 'medium' | 'small';
  icon?: IconName;
}

export function Button({ variant = 'primary', size = 'medium', icon, className = '', type = 'button', children, ...props }: ButtonProps) {
  return <button {...props} type={type} className={`button button--${variant}${size === 'small' ? ' button--small' : ''} ${className}`}>
    {icon && <Icon name={icon} />}{children}
  </button>;
}
