import functools
import inspect
import json
import logging
from typing import Callable, Generic, TypeVar, Union

import marvin
import marvin.utilities.tools
import prefect
from marvin.beta.assistants import Thread
from marvin.beta.assistants.assistants import Assistant
from marvin.beta.assistants.handlers import PrintHandler
from marvin.beta.assistants.runs import Run
from marvin.tools.assistants import AssistantTool, EndRun
from marvin.types import FunctionTool
from marvin.utilities.asyncio import ExposeSyncMethodsMixin, expose_sync_method
from marvin.utilities.jinja import Environment
from openai.types.beta.threads.runs import ToolCall
from prefect import get_client as get_prefect_client
from prefect import task as prefect_task
from prefect.context import FlowRunContext
from pydantic import BaseModel, Field, field_validator

from control_flow import settings
from control_flow.context import ctx
from control_flow.utilities.prefect import (
    create_json_artifact,
    create_markdown_artifact,
    create_python_artifact,
)

from .flow import AIFlow
from .task import AITask, TaskStatus

T = TypeVar("T")
logger = logging.getLogger(__name__)

NOT_PROVIDED = object()
TEMP_THREADS = {}


TOOL_CALL_FUNCTION_RESULT_TEMPLATE = inspect.cleandoc(
    """
    ## Tool call: {name}
    
    **Description:** {description}
    
    ## Arguments
    
    ```json
    {args}
    ```
    
    ### Result
    
    ```json
    {result}
    ```
    """
)


INSTRUCTIONS = """
You are an AI assistant. Your job is to complete the tasks assigned to you.  You
were created by a software application, and any messages you receive are from
that software application, not a user. You may use any tools at your disposal to
complete the task, including talking to a human user.


## Instructions

Follow these instructions at all times:

{% if assistant.instructions -%}
- {{ assistant.instructions }}
{% endif %}
{% if flow.instructions -%}
- {{ flow.instructions }}
{% endif %}
{% if agent.instructions -%}
- {{ agent.instructions }}
{% endif %}
{% for instruction in instructions %}
- {{ instruction }}
{% endfor %}


## Tasks

{% if agent.tasks %}
You have been assigned the following tasks. You will continue to run until all
tasks are finished. It may take multiple attempts, iterations, or tool uses to
complete a task. When a task is finished, mark it as `completed`
(and provide a result, if required) or `failed` (with a brief explanation) by
using the appropriate tool. Do not mark a task as complete if you don't have a
complete result. Do not make up results. If you receive only partial or unclear
information from a user, keep working until you have all the information you
need. Be very sure that a task is truly unsolvable before marking it as failed,
especially when working with a human user.


{% for task_id, task in agent.numbered_tasks() %}
### Task {{ task_id }}
- Status: {{ task.status.value }}
- Objective: {{ task.objective }}
{% if task.instructions %}
- Additional instructions: {{ task.instructions }}
{% endif %}
{% if task.status.value == "completed" %}
- Result: {{ task.result }}
{% elif task.status.value == "failed" %}
- Error: {{ task.error }}
{% endif %}
{% if task.context %}
- Context: {{ task.context }}
{% endif %}

{% endfor %}
{% else %}
You have no explicit tasks to complete. Follow your instructions as best as you
can. If it is not possible to comply with the instructions in any way, use the
`end_run` tool to manually stop the run.
{% endif %}

## Communication

All messages you receive in the thread are generated by the software that
created you, not a human user. All messages you send are sent only to that
software and are never seen by any human.

{% if agent.system_access -%}
The software that created you is an AI capable of processing natural language,
so you can freely respond by posting messages to the thread.
{% else %}
The software that created you is a Python script that can only process
structured responses produced by your tools. DO NOT POST ANY MESSAGES OR RESPONSES TO THE
THREAD. They will be ignored and only waste time. ONLY USE TOOLS TO RESPOND.
{% endif %}

{% if agent.user_access -%}
There is also a human user who may be involved in the task. You can communicate
with them using the `talk_to_human` tool. The user is a human and unaware of
your tasks or this system. Do not mention your tasks or anything about how the
system works to them. They can only see messages you send them via tool, not the
rest of the thread. When dealing with humans, you may not always get a clear or
correct response. You may need to ask multiple times or rephrase your questions.
You should also interpret human responses broadly and not be too literal.
{% else %}
You can not communicate with a human user at this time.
{% endif %}


{% if context %}
## Additional context

The following context was provided:
{% for key, value in context.items() -%}
- {{ key }}: {{ value }}
{% endfor %}
{% endif %}
"""


class AgentHandler(PrintHandler):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.tool_calls = {}

    async def on_tool_call_created(self, tool_call: ToolCall) -> None:
        """Callback that is fired when a tool call is created"""

        if tool_call.type == "function":
            task_run_name = "Prepare arguments for tool call"
        else:
            task_run_name = f"Tool call: {tool_call.type}"

        client = get_prefect_client()
        engine_context = FlowRunContext.get()
        if not engine_context:
            return

        task_run = await client.create_task_run(
            task=prefect.Task(fn=lambda: None),
            name=task_run_name,
            extra_tags=["tool-call"],
            flow_run_id=engine_context.flow_run.id,
            dynamic_key=tool_call.id,
            state=prefect.states.Running(),
        )

        self.tool_calls[tool_call.id] = task_run

    async def on_tool_call_done(self, tool_call: ToolCall) -> None:
        """Callback that is fired when a tool call is done"""

        client = get_prefect_client()
        task_run = self.tool_calls.get(tool_call.id)
        if not task_run:
            return
        await client.set_task_run_state(
            task_run_id=task_run.id, state=prefect.states.Completed(), force=True
        )

        # code interpreter is run as a single call, so we can publish a result artifact
        if tool_call.type == "code_interpreter":
            # images = []
            # for output in tool_call.code_interpreter.outputs:
            #     if output.type == "image":
            #         image_path = download_temp_file(output.image.file_id)
            #         images.append(image_path)

            create_python_artifact(
                key="code",
                code=tool_call.code_interpreter.input,
                description="Code executed in the code interpreter",
                task_run_id=task_run.id,
            )
            create_json_artifact(
                key="output",
                data=tool_call.code_interpreter.outputs,
                description="Output from the code interpreter",
                task_run_id=task_run.id,
            )

        elif tool_call.type == "function":
            create_json_artifact(
                key="arguments",
                data=json.dumps(json.loads(tool_call.function.arguments), indent=2),
                description=f"Arguments for the `{tool_call.function.name}` tool",
                task_run_id=task_run.id,
            )


def talk_to_human(message: str, get_response: bool = True) -> str:
    """
    Send a message to the human user and optionally wait for a response.
    If `get_response` is True, the function will return the user's response,
    otherwise it will return a simple confirmation.
    """
    print(message)
    if get_response:
        response = input("> ")
        return response
    return "Message sent to user"


def end_run():
    """Use this tool to end the run."""
    return EndRun()


class Agent(BaseModel, Generic[T], ExposeSyncMethodsMixin):
    tasks: list[AITask] = []
    flow: AIFlow = Field(None, validate_default=True)
    assistant: Assistant = Field(None, validate_default=True)
    tools: list[Union[AssistantTool, Assistant, Callable]] = []
    context: dict = Field(None, validate_default=True)
    user_access: bool = Field(
        None,
        validate_default=True,
        description="If True, the agent is given tools for interacting with a human user.",
    )
    system_access: bool = Field(
        None,
        validate_default=True,
        description="If True, the agent will communicate with the system via messages. "
        "This is usually only used when the agent was spawned by another "
        "agent capable of understanding its responses.",
    )
    instructions: str = None
    model_config: dict = dict(arbitrary_types_allowed=True, extra="forbid")

    @field_validator("flow", mode="before")
    def _load_flow_from_ctx(cls, v):
        if v is None:
            v = ctx.get("flow", None)
            if v is None:
                v = AIFlow()
        return v

    @field_validator("context", mode="before")
    def _default_context(cls, v):
        if v is None:
            v = {}
        return v

    @field_validator("assistant", mode="before")
    def _default_assistant(cls, v):
        if v is None:
            flow = ctx.get("flow")
            if flow:
                v = flow.assistant
            if v is None:
                v = Assistant()
        return v

    @field_validator("user_access", "system_access", mode="before")
    def _default_access(cls, v):
        if v is None:
            v = False
        return v

    def numbered_tasks(self) -> list[tuple[int, AITask]]:
        return [(i + 1, task) for i, task in enumerate(self.tasks)]

    def _get_instructions(self, context: dict = None):
        instructions = Environment.render(
            INSTRUCTIONS,
            agent=self,
            flow=self.flow,
            assistant=self.assistant,
            instructions=ctx.get("instructions", []),
            context={**self.context, **(context or {})},
        )

        return instructions

    def _get_tools(self) -> list[AssistantTool]:
        tools = self.flow.tools + self.tools + self.assistant.tools

        if not self.tasks:
            tools.append(end_run)

        # if there is only one task, and the agent can't send a response to the
        # system, then we can quit as soon as it is marked finished
        if not self.system_access and len(self.tasks) == 1:
            early_end_run = True
        else:
            early_end_run = False

        for i, task in self.numbered_tasks():
            tools.extend(
                [
                    task._create_complete_tool(task_id=i, end_run=early_end_run),
                    task._create_fail_tool(task_id=i, end_run=early_end_run),
                ]
            )

        if self.user_access:
            tools.append(talk_to_human)

        final_tools = []
        for tool in tools:
            if isinstance(tool, marvin.beta.assistants.Assistant):
                tool = self.model_copy(update={"assistant": tool}).as_tool()
            elif not isinstance(tool, AssistantTool):
                tool = marvin.utilities.tools.tool_from_function(tool)

            if isinstance(tool, FunctionTool):

                async def modified_fn(
                    *args,
                    # provide default args to avoid a late-binding issue
                    original_fn: Callable = tool.function._python_fn,
                    tool: FunctionTool = tool,
                    **kwargs,
                ):
                    # call fn
                    result = original_fn(*args, **kwargs)

                    passed_args = (
                        inspect.signature(original_fn).bind(*args, **kwargs).arguments
                    )
                    try:
                        passed_args = json.dumps(passed_args, indent=2)
                    except Exception:
                        pass
                    create_markdown_artifact(
                        markdown=TOOL_CALL_FUNCTION_RESULT_TEMPLATE.format(
                            name=tool.function.name,
                            description=tool.function.description or "(none provided)",
                            args=passed_args,
                            result=result,
                        ),
                        key="result",
                    )
                    return result

                tool.function._python_fn = prefect_task(
                    modified_fn,
                    task_run_name=f"Tool call: {tool.function.name}",
                )
            final_tools.append(tool)
        return final_tools

    def _get_openai_run_task(self):
        """
        Helper function for building the task that will execute the OpenAI assistant run.
        This needs to be regenerated each time in case the instructions change.
        """

        @prefect_task(task_run_name=f"Run OpenAI assistant ({self.assistant.name})")
        async def execute_openai_run(
            context: dict = None, run_kwargs: dict = None
        ) -> Run:
            run_kwargs = run_kwargs or {}
            model = run_kwargs.pop(
                "model",
                self.assistant.model or self.flow.model or settings.assistant_model,
            )
            thread = run_kwargs.pop("thread", self.flow.thread)

            run = Run(
                assistant=self.assistant,
                thread=thread,
                instructions=self._get_instructions(context=context),
                tools=self._get_tools(),
                event_handler_class=AgentHandler,
                model=model,
                **run_kwargs,
            )
            await run.run_async()
            create_json_artifact(
                key="messages",
                # dump explicilty because of odd OAI serialization issue
                data=[m.model_dump() for m in run.messages],
                description="All messages sent and received during the run.",
            )
            create_json_artifact(
                key="actions",
                # dump explicilty because of odd OAI serialization issue
                data=[s.model_dump() for s in run.steps],
                description="All actions taken by the assistant during the run.",
            )
            return run

        return execute_openai_run

    @expose_sync_method("run")
    async def run_async(self, context: dict = None, **run_kwargs) -> list[AITask]:
        openai_run = self._get_openai_run_task()

        openai_run(context=context, run_kwargs=run_kwargs)

        # if this AI can't post messages to the system, then continue to invoke
        # it until all tasks are finished
        if not self.system_access:
            counter = 0
            while (
                any(t.status == TaskStatus.PENDING for t in self.tasks)
                and counter < settings.max_agent_iterations
            ):
                openai_run(context=context, run_kwargs=run_kwargs)
                counter += 1

        result = [t.result for t in self.tasks if t.status == TaskStatus.COMPLETED]

        return result

    def as_tool(self):
        thread = TEMP_THREADS.setdefault(self.assistant.model_dump_json(), Thread())

        def _run(message: str, context: dict = None) -> list[str]:
            task = self._get_openai_run_task()
            run: Run = task(context=context, run_kwargs=dict(thread=thread))
            return [m.model_dump_json() for m in run.messages]

        return marvin.utilities.tools.tool_from_function(
            _run,
            name=f"call_ai_{self.assistant.name}",
            description=inspect.cleandoc("""
            Use this tool to talk to a sub-AI that can operate independently of
            you. The sub-AI may have a different skillset or be able to access
            different tools than you. The sub-AI will run one iteration and
            respond to you. You may continue to invoke it multiple times in sequence, as
            needed. 
            
            Note: you can only talk to one sub-AI at a time. Do not call in parallel or you will get an error about thread conflicts.
            
            ## Sub-AI Details
            
            - Name: {name}
            - Instructions: {instructions}
            """).format(
                name=self.assistant.name, instructions=self.assistant.instructions
            ),
        )


def ai_task(
    fn=None, *, objective: str = None, user_access: bool = None, **agent_kwargs: dict
):
    """
    Decorator that uses a function to create an AI task. When the function is
    called, an agent is created to complete the task and return the result.
    """

    if fn is None:
        return functools.partial(
            ai_task, objective=objective, user_access=user_access, **agent_kwargs
        )

    sig = inspect.signature(fn)

    if objective is None:
        if fn.__doc__:
            objective = f"{fn.__name__}: {fn.__doc__}"
        else:
            objective = fn.__name__

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        # first process callargs
        bound = sig.bind(*args, **kwargs)
        bound.apply_defaults()

        return run_agent.with_options(name=f"Task: {fn.__name__}")(
            task=objective,
            cast=fn.__annotations__.get("return"),
            context=bound.arguments,
            user_access=user_access,
            **agent_kwargs,
        )

    return wrapper


def _name_from_objective():
    """Helper function for naming task runs"""
    from prefect.runtime import task_run

    objective = task_run.parameters.get("task")

    if not objective:
        objective = "Follow general instructions"
    if len(objective) > 75:
        return f"Task: {objective[:75]}..."
    return f"Task: {objective}"


@prefect_task(task_run_name=_name_from_objective)
def run_agent(
    task: str = None,
    cast: T = NOT_PROVIDED,
    context: dict = None,
    user_access: bool = None,
    model: str = None,
    **agent_kwargs: dict,
) -> T:
    """
    Run an agent to complete a task with the given objective and context. The
    response will be cast to the given result type.
    """

    if cast is NOT_PROVIDED:
        if not task:
            cast = None
        else:
            cast = str

    # load flow
    flow = ctx.get("flow", None)

    # create tasks
    if task:
        ai_tasks = [AITask[cast](objective=task, context=context)]
    else:
        ai_tasks = []

    # run agent
    agent = Agent(tasks=ai_tasks, flow=flow, user_access=user_access, **agent_kwargs)
    agent.run(model=model)

    if ai_tasks:
        if ai_tasks[0].status == TaskStatus.COMPLETED:
            return ai_tasks[0].result
        elif ai_tasks[0].status == TaskStatus.FAILED:
            raise ValueError(ai_tasks[0].error)
