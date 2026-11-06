import { Link } from 'react-router';
import { EmptyState, Icon } from '../components';
import { copy } from '../copy';
import './Service.css';

export function NotFoundPage() {
  return (
    <div className="service-center">
      <section className="panel service-panel">
        <EmptyState level={1} icon="pilot" title={copy.service.notFoundTitle} description={copy.service.notFoundDescription}
          action={<Link className="button button--primary" to="/"><Icon name="home" />{copy.service.goHome}</Link>} />
      </section>
    </div>
  );
}
