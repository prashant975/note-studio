"""The Queue card's buttons, and a page that shows the whole line (job_queue).

Under /work-queue: /queue is the older "Send to bots" screen."""
from urllib.parse import urlparse

from fastapi import Request

import job_queue


def back(request):
    """The screen the button was pressed on (the card sits on several), never another site."""
    path = urlparse(request.headers.get('referer') or '').path
    return path if path.startswith('/') and not path.startswith('//') and not path.startswith('/work-queue/') else '/work-queue'


def register(app, render, go):
    @app.get('/work-queue')
    def page(request: Request):
        return render(request, 'work_queue.html', queue=job_queue.overview())

    @app.get('/work-queue/card')
    def card(request: Request):
        return render(request, '_queue_card.html', queue=job_queue.overview())

    @app.post('/work-queue/{item_id}/remove')
    def remove(request: Request, item_id: int):
        try:
            item = job_queue.remove(item_id)
        except ValueError as exc:
            return go(back(request), str(exc), 'warn')
        return go(back(request), '"%s" was removed from the queue. Nothing had been created for it.' % item['title'], 'info')

    @app.post('/work-queue/pause')
    def pause(request: Request):
        job_queue.pause('You put the queue on hold. What is running now carries on; nothing new starts.')
        return go(back(request), 'The queue is on hold. What is running now carries on.', 'info')

    @app.post('/work-queue/continue')
    def carry_on(request: Request):
        job_queue.resume()
        job_queue.tick()
        return go(back(request), 'The queue continues.', 'info')

    @app.post('/work-queue/clear')
    def clear(request: Request):
        count = job_queue.clear()
        return go(back(request), '%d waiting item%s removed from the queue.' % (count, '' if count == 1 else 's'), 'info')
