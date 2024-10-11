import time
import uuid
from collections import defaultdict
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import fakeredis
import pytest
import pytest_asyncio
from langgraph.checkpoint.base import Checkpoint
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from pydantic_core import from_json
from redis import ConnectionError
from redis.asyncio import ConnectionPool, Redis

from agents.memory.conversation_history import ConversationMessage, QueryType
from agents.memory.redis_checkpointer import (
    JsonAndBinarySerializer,
    RedisSaver,
    get_async_connection,
    initialize_async_pool,
)


@pytest.mark.parametrize(
    "url, kwargs, expected_pool, expected_exception",
    [
        ("redis://localhost", {}, AsyncMock(spec=ConnectionPool), None),
        (
            "redis://testhost:6379",
            {"max_connections": 10},
            AsyncMock(spec=ConnectionPool),
            None,
        ),
        ("invalid-url", {}, None, ValueError),
    ],
)
@patch("redis.asyncio.ConnectionPool.from_url")
def test_initialize_async_pool(
    mock_from_url, url, kwargs, expected_pool, expected_exception
):
    if expected_exception:
        mock_from_url.side_effect = ValueError("Invalid URL")
        with pytest.raises(expected_exception):
            initialize_async_pool(url, **kwargs)
    else:
        mock_from_url.return_value = expected_pool
        result = initialize_async_pool(url, **kwargs)

        mock_from_url.assert_called_once_with(url, **kwargs)
        assert isinstance(result, ConnectionPool)
        assert result == expected_pool


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "connection, expected_type, expected_exception",
    [
        (AsyncMock(spec=Redis), Redis, None),
        (
            AsyncMock(spec=ConnectionPool, connection_kwargs={"protocol": 3}),
            Redis,
            None,
        ),
        (None, None, ValueError),
        ("invalid_connection", None, ValueError),
    ],
)
async def test_get_async_connection(connection, expected_type, expected_exception):
    if expected_exception:
        with pytest.raises(expected_exception):
            async with get_async_connection(connection):
                pass
    else:
        async with get_async_connection(connection) as conn:
            assert isinstance(conn, expected_type)


@pytest.mark.asyncio
@patch("redis.asyncio.Redis.__init__", side_effect=ConnectionError("Connection failed"))
async def test_get_async_connection_connection_error(mock_redis):
    with pytest.raises(ConnectionError, match="Connection failed"):
        async with get_async_connection(AsyncMock(spec=ConnectionPool)):
            pass


def create_checkpoint(checkpoint_id):
    return Checkpoint(
        v=1,
        id=checkpoint_id,
        ts=datetime.now(UTC).isoformat(),
        channel_values={},
        channel_versions={},
        versions_seen=defaultdict(dict),
        pending_sends=[],
    )


def create_metadata(step):
    return {"source": "input", "step": step, "writes": {}, "score": 1}


class TestJsonAndBinarySerializer:

    @pytest.fixture
    def serializer(self):
        return JsonAndBinarySerializer()

    class UnserializableObject:
        pass

    @pytest.mark.parametrize(
        "input_data, expected_output, is_binary, expected_exception",
        [
            ({"key": "value"}, b'{"key": "value"}', False, None),
            (b"hello", "68656c6c6f", True, None),
            (bytearray(b"world"), "776f726c64", True, None),
            ([1, 2, 3], b"[1, 2, 3]", False, None),
            (UnserializableObject(), None, False, Exception),
        ],
    )
    def test_dumps(
        self, serializer, input_data, expected_output, is_binary, expected_exception
    ):
        if expected_exception:
            with pytest.raises(expected_exception):  # noqa E722
                serializer.dumps(self.UnserializableObject())
        else:
            result = serializer.dumps(input_data)
            assert result == expected_output

    @pytest.mark.parametrize(
        "input_data, expected_output, is_binary, expected_exception",
        [
            ('{"key": "value"}', {"key": "value"}, False, None),
            ("68656c6c6f", b"hello", True, None),
            ("776f726c64", b"world", True, None),
            ("[1, 2, 3]", [1, 2, 3], False, None),
            ("invalid json", None, False, Exception),
        ],
    )
    def test_loads(
        self, serializer, input_data, expected_output, is_binary, expected_exception
    ):
        if expected_exception:
            with pytest.raises(expected_exception):  # noqa E722
                serializer.loads("invalid json")
        else:
            result = serializer.loads(input_data, is_binary=is_binary)
            assert result == expected_output


@pytest.mark.asyncio
class TestRedisSaver:
    serde = JsonPlusSerializer()

    @pytest_asyncio.fixture
    async def fake_async_redis(self):
        async with fakeredis.FakeAsyncRedis() as client:
            yield client

    @pytest_asyncio.fixture(autouse=True)
    def setup(self, fake_async_redis):
        self.redis_saver = RedisSaver(async_connection=fake_async_redis)

    @pytest.mark.parametrize(
        "config, checkpoint, metadata, expected_parent_ts",
        [
            (
                {"configurable": {"thread_id": "thread-1"}},
                "chk-1",
                create_metadata(1),
                "",
            ),
            (
                {"configurable": {"thread_id": "thread-1", "thread_ts": "chk-1"}},
                "chk-2",
                create_metadata(2),
                "chk-1",
            ),
        ],
    )
    async def test_aput(
        self, config, checkpoint, metadata, expected_parent_ts, fake_async_redis
    ):
        checkpoint_obj = create_checkpoint(checkpoint)
        await self.redis_saver.aput(config, checkpoint_obj, metadata, {})

        key = f"checkpoint:{config['configurable']['thread_id']}:{checkpoint}"
        actual_result = await fake_async_redis.hgetall(key)

        assert self.serde.loads(actual_result[b"checkpoint"].decode()) == checkpoint_obj
        assert self.serde.loads(actual_result[b"metadata"].decode()) == metadata
        assert actual_result[b"parent_ts"].decode() == expected_parent_ts

    @pytest.mark.parametrize(
        "put_config, get_config, checkpoints, metadata, expected_checkpoint",
        [
            (
                {"configurable": {"thread_id": "thread-1"}},
                {"configurable": {"thread_id": "thread-1", "thread_ts": "chk-1"}},
                ["chk-1"],
                create_metadata(1),
                "chk-1",
            ),
            (
                {"configurable": {"thread_id": "thread-1", "thread_ts": "chk-1"}},
                {"configurable": {"thread_id": "thread-1", "thread_ts": "chk-2"}},
                ["chk-2"],
                create_metadata(2),
                "chk-2",
            ),
            (
                {"configurable": {"thread_id": "thread-1", "thread_ts": "chk-2"}},
                {"configurable": {"thread_id": "thread-1"}},
                ["chk-1", "chk-2", "chk-3"],
                create_metadata(3),
                "chk-3",
            ),
        ],
    )
    async def test_aget(
        self, put_config, get_config, checkpoints, metadata, expected_checkpoint
    ):
        for chk in checkpoints:
            await self.redis_saver.aput(
                put_config, create_checkpoint(chk), metadata, {}
            )

        saved_data = await self.redis_saver.aget_tuple(get_config)

        assert saved_data.checkpoint["id"] == expected_checkpoint
        assert saved_data.metadata == metadata
        assert saved_data.parent_config == (
            put_config if put_config["configurable"].get("thread_ts") else None
        )

    @pytest.mark.parametrize(
        "test_description, conversation_id, expected_error_type",
        [
            (
                "should return error if conversation_id is empty string",
                "",
                ValueError,
            ),
            (
                "should succeed to save message",
                str(uuid.uuid4()),
                None,
            ),
        ],
    )
    async def test_add_conversation_message(
        self,
        test_description,
        conversation_id,
        expected_error_type,
        fake_async_redis,
    ):
        given_message = ConversationMessage(
            type=QueryType.INITIAL_QUESTIONS,
            query="",
            response="list of initial questions",
            timestamp=time.time(),
        )
        # error cases.
        if expected_error_type is not None:
            with pytest.raises(expected_error_type):
                await self.redis_saver.add_conversation_message(
                    conversation_id, given_message
                )
            # exit the test.
            return

        # normal test cases.
        # given
        # the redis store should be empty.
        key = f"history:{conversation_id}"
        actual_count = await fake_async_redis.llen(key)
        assert actual_count == 0

        # when
        await self.redis_saver.add_conversation_message(conversation_id, given_message)

        # then
        # verify that the redis store have one message in storage.
        actual_count = await fake_async_redis.llen(key)
        assert actual_count == 1
        # get and compare the actual message.
        messages = await fake_async_redis.lrange(key, 0, actual_count)
        assert len(messages) == 1
        got_message = ConversationMessage.model_validate(
            from_json(messages[0], allow_partial=True)
        )
        assert got_message == given_message

    @pytest.mark.parametrize(
        "test_description, conversation_id, given_messages",
        [
            (
                "should return zero messages if the conversation is empty",
                str(uuid.uuid4()),
                [],
            ),
            (
                "should return two messages",
                str(uuid.uuid4()),
                [
                    ConversationMessage(
                        type=QueryType.INITIAL_QUESTIONS,
                        query="",
                        response="list of initial questions",
                        timestamp=time.time(),
                    ),
                    ConversationMessage(
                        type=QueryType.USER_QUERY,
                        query="What is kubernetes?",
                        response="it is a kind of container orchestration tool",
                        timestamp=time.time(),
                    ),
                ],
            ),
        ],
    )
    async def test_get_all_conversation_messages(
        self,
        test_description,
        conversation_id,
        given_messages,
        fake_async_redis,
    ):
        # given
        # add given messages to redis store.
        for message in given_messages:
            await self.redis_saver.add_conversation_message(conversation_id, message)

        # when
        got_messages = await self.redis_saver.get_all_conversation_messages(
            conversation_id
        )

        # then
        assert len(got_messages) == len(given_messages)
