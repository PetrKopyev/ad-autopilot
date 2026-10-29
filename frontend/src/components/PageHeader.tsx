import type { ReactNode } from 'react';

export interface PageHeaderProps {
  title: string;
  /** Одна строка под заголовком: бизнес и город, период. */
  description?: ReactNode;
  /** Справа: режим автопилота, «Остановить всю рекламу», шаги мастера. На телефоне уходят под заголовок. */
  actions?: ReactNode;
}

/** Белая шапка страницы во всю ширину рабочей области, как в макете C. Должна быть первой на странице. */
export function PageHeader({ title, description, actions }: PageHeaderProps) {
  return <header className="page-header">
    <div className="page-header__title"><h1>{title}</h1>{description && <p className="page-header__description">{description}</p>}</div>
    {actions && <div className="page-header__actions">{actions}</div>}
  </header>;
}
