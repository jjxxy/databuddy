"""Actual per-slide construction progress for interactive Office tool calls.

These previews are extracted from constructed shapes; they are not a rendering
of the slide, a final artifact, or a financial/visual validation receipt.
"""
from sqlalchemy import select, update
from contextlib import contextmanager
import re
from . import store

_MAX_PREVIEW_CHARS = 2000


def _slide_preview(number, slide):
    title = slide.shapes.title.text if slide.shapes.title is not None else ''
    lines = []
    size = 0
    omitted = False

    def add(value):
        nonlocal size, omitted
        if not isinstance(value, str) or not value:
            return
        # Never cut a numeric value or a row in half. The UI discloses omissions.
        if size + len(value) > _MAX_PREVIEW_CHARS:
            omitted = True
            return
        lines.append(value)
        size += len(value)

    shapes=list(slide.shapes)
    skip_next=False
    for index,shape in enumerate(shapes):
        if skip_next:
            skip_next=False
            continue
        if shape.has_table:
            following=shapes[index+1] if index+1<len(shapes) else None
            if (following is not None and following.has_text_frame
                    and re.fullmatch(r'完整数据：\d+ 行 · 本页 \d+–\d+ 行 · 列组 \d+/\d+',following.text)):
                # Put the real page scope before any preview rows so even a
                # long table does not hide that this is only one portion.
                add(following.text);skip_next=True
            for row in shape.table.rows:
                add(' | '.join(cell.text for cell in row.cells))
        elif shape.has_text_frame and shape.name.startswith((
                'nexus:text:', 'nexus:chart-title:', 'nexus:chart-unit:',
                'nexus:chart-label:', 'nexus:chart-value:')):
            # The chart's label and exact original value are created as
            # adjacent, independently editable shapes. Only pair matching
            # identifiers; never guess a value from a chart or another row.
            suffix=shape.name.removeprefix('nexus:chart-label:') if shape.name.startswith('nexus:chart-label:') else None
            following=shapes[index+1] if suffix is not None and index+1<len(shapes) else None
            if following is not None and following.has_text_frame and following.name=='nexus:chart-value:'+suffix:
                add(shape.text+'：'+following.text)
                skip_next=True
            else:add(shape.text)
    return {'number': number, 'title': title, 'lines': lines, 'omitted': omitted}


class SlideProgress:
    def __init__(self, turn, step_id, tool='render_office_artifact'):
        self.turn_id = turn['id']
        self.project_id = turn['project_id']
        self.step_id = step_id
        self.tool = tool
        self.actor_id = turn.get('actor_id','owner')
        self.session_id = turn.get('session_id')
        self.gateway_client_id = turn.get('gateway_client_id')
        self.pages = []
        self.total = None

    def _update(self, progress, *, write=True):
        # Cancellation and step changes use the same authoritative row. Lock
        # and verify it in the transaction that publishes preview metadata.
        from .locks import assert_owned
        with store.engine.connect() as connection:
            if store.engine.dialect.name == 'sqlite':
                connection.exec_driver_sql('BEGIN IMMEDIATE')
            else:
                connection.begin()
            try:
                row=connection.execute(select(store.items.c.body).where(
                    store.items.c.kind=='turn',store.items.c.id==self.turn_id).with_for_update()).first()
                if not row:raise ValueError('演示文稿生成任务已停止或发生变化')
                current=store._decode(row[0]);trace=current.get('agent_trace') or []
                if (current.get('project_id')!=self.project_id or current.get('actor_id','owner')!=self.actor_id
                        or current.get('session_id')!=self.session_id or current.get('state')!='agent_tool'
                        or current.get('outcome')=='cancelled' or not trace or not isinstance(trace[-1],dict)
                        or trace[-1].get('id')!=self.step_id or trace[-1].get('tool')!=self.tool
                        or trace[-1].get('status')!='started'):
                    raise ValueError('演示文稿生成任务已停止或发生变化')
                if write:
                    current['office_slide_progress']=progress
                    connection.execute(update(store.items).where(store.items.c.kind=='turn',
                        store.items.c.id==self.turn_id).values(body=store._encode('turn',current,connection)))
                assert_owned()
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def begin(self):
        # Prevent old previews from being shown for a cache hit or retry.
        self._update(None)

    def check_live(self):
        self._update(None,write=False)

    @contextmanager
    def publication_guard(self):
        """Serialize the actual receipt/rename with Stop and workspace removal.

        The caller already holds the workspace lock; lifecycle removal takes
        the project lock, while owner Stop takes turn_guard and gateway Stop
        takes client_guard. A later Stop cannot undo an already committed
        artifact.
        """
        from .job_authority import turn_guard,client_guard
        from .workspace_lifecycle import _project_lock,assert_turn_live
        from .locks import assert_owned
        from contextlib import ExitStack
        with ExitStack() as stack:
            # Gateway admission may hold client while acquiring workspace.
            # We already hold workspace, so never wait for that client lock:
            # a busy grant fails closed and the durable pending result can
            # later be retried. Owner Stop never takes the lifecycle lock.
            if self.gateway_client_id:
                stack.enter_context(client_guard(self.gateway_client_id,timeout=0))
                stack.enter_context(_project_lock(self.project_id))
            else:
                stack.enter_context(_project_lock(self.project_id))
                stack.enter_context(turn_guard(self.turn_id))
            current=store.get('turn',self.turn_id)
            assert_turn_live(current)
            if current.get('gateway_client_id')!=self.gateway_client_id:
                raise ValueError('演示文稿生成任务已停止或发生变化')
            if self.gateway_client_id:
                from .agent_gateway import assert_turn_allowed
                assert_turn_allowed(current)
            self.check_live()
            yield
            assert_owned()

    def page(self, number, total, slide):
        if (type(number) is not int or type(total) is not int or not 1 <= number <= total <= 120
                or number != len(self.pages) + 1 or (self.total is not None and total != self.total)):
            raise ValueError('演示文稿页进度不一致，未交付')
        self.total = total
        self.pages.append(_slide_preview(number, slide))
        self._update({'step_id': self.step_id, 'total': total, 'pages': list(self.pages), 'phase': 'building'})

    def finish(self, success):
        if not self.pages:
            return
        try:
            phase = 'verified' if success and len(self.pages) == self.total else 'interrupted'
            self._update({'step_id': self.step_id, 'total': self.total, 'pages': list(self.pages), 'phase': phase})
        except (KeyError, ValueError):
            # A cancelled or replaced task must not be resurrected or rewritten.
            pass


def for_turn(turn, *, tool='render_office_artifact'):
    trace = turn.get('agent_trace') or []
    if (turn.get('state') != 'agent_tool' or not trace or not isinstance(trace[-1], dict)
            or trace[-1].get('tool') != tool
            or trace[-1].get('status') != 'started' or not isinstance(trace[-1].get('id'), str)
            or not turn.get('id') or not turn.get('project_id')):
        return None
    return SlideProgress(turn, trace[-1]['id'],tool)
