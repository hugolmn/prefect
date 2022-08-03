from prefect import flow, get_run_logger, task

purpose = """
The purpose of this flow is to see how Prefect handles a flow
with multiple tasks if it fails after executing one task, but 
has further dependent tasks.

Expected behavior: TODO
"""


@task
def parent_task():
    return 256


@task
def first_child(from_parent):
    raise Exception("Child task intentionally failed for testing purposes")


@task
def second_child(from_parent):
    return 42


@task
def grand_child(from_child):
    return 512


@flow
def fails_in_middle_1():
    logger = get_run_logger()
    logger.info(purpose)

    parent_res = parent_task()
    c1_res = first_child(parent_res)
    c2_res = second_child(parent_res)
    grand_child(c1_res)


if __name__ == "__main__":
    fails_in_middle_1()