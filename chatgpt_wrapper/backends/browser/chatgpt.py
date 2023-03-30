import os
import asyncio
import base64
import json
import time
import datetime
import uuid
import re
import shutil
from typing import Optional
from playwright.async_api import async_playwright
from playwright._impl._api_structures import ProxySettings

from chatgpt_wrapper.core.backend import Backend
from chatgpt_wrapper.core.config import Config
from chatgpt_wrapper.core.logger import Logger
import chatgpt_wrapper.core.constants as constants

GEN_TITLE_TIMEOUT = 5000

class ChatGPTLLM:
    def __init__(self, chatgpt):
        self.chatgpt = chatgpt

    def __call__(self):
        return self.chatgpt.ask()

class AsyncChatGPT(Backend):
    """
    A ChatGPT interface that uses Playwright to run a browser,
    and interacts with that browser to communicate with ChatGPT in
    order to provide an open API to ChatGPT.
    """

    stream_div_id = "chatgpt-wrapper-conversation-stream-data"
    eof_div_id = "chatgpt-wrapper-conversation-stream-data-eof"
    interrupt_div_id = "chatgpt-wrapper-conversation-stream-data-interrupt"
    session_div_id = "chatgpt-wrapper-session-data"

    def __init__(self, config=None):
        super().__init__(config)
        self.llm_class = ChatGPTLLM(self)
        self.play = None
        self.user_data_dir = None
        self.page = None
        self.browser = None
        self.session = None
        self.new_conversation()

    def get_primary_profile_directory(self):
        primary_profile = os.path.join(self.config.data_profile_dir, "playwright")
        return primary_profile

    async def create(self, timeout=60, proxy: Optional[ProxySettings] = None):
        primary_profile = self.get_primary_profile_directory()
        self.streaming = False
        self.lock = asyncio.Lock()
        self.play = await async_playwright().start()
        browser = self.config.get('browser.provider')
        headless = not self.config.get('browser.debug')
        try:
            playbrowser = getattr(self.play, browser)
        except Exception:
            print(f"Browser {browser} is invalid, falling back on firefox")
            playbrowser = self.play.firefox
        try:
            self.browser = await playbrowser.launch_persistent_context(
                user_data_dir=primary_profile,
                headless=headless,
                proxy=proxy,
            )
        except Exception:
            self.user_data_dir = f"{primary_profile}-{str(uuid.uuid4())}"
            message = f"Unable to launch browser from primary profile, trying alternate profile {self.user_data_dir}"
            print(message)
            self.log.warning(message)
            shutil.copytree(primary_profile, self.user_data_dir, ignore=shutil.ignore_patterns("lock"))
            self.browser = await playbrowser.launch_persistent_context(
                user_data_dir=self.user_data_dir,
                headless=headless,
                proxy=proxy,
            )

        if len(self.browser.pages) > 0:
            self.page = self.browser.pages[0]
        else:
            self.page = await self.browser.new_page()
        await self._start_browser()
        self.timeout = timeout
        self.log.info("ChatGPT initialized")
        return self

    def destroy_primary_profile(self):
        primary_profile = self.get_primary_profile_directory()
        shutil.rmtree(primary_profile)
        message = f"Destroyed primary profile: {primary_profile}"
        print(message)
        self.log.info(message)

    def _shutdown(self):
        loop = asyncio.get_event_loop()
        loop.run_until_complete(asyncio.gather(self.cleanup()))

    async def _start_browser(self):
        await self.page.goto("https://chat.openai.com/")

    def _handle_error(self, obj, response, message):
        full_message = f"{message}: {response.status} {response.status_text}"
        self.log.error(full_message)
        return False, obj, full_message

    async def cleanup(self):
        self.log.info("Cleaning up")
        await self.browser.close()
        # remove the user data dir in case this is a second instance
        if self.user_data_dir:
            shutil.rmtree(self.user_data_dir)
        await self.play.stop()

    def get_backend_name(self):
        return "chatgpt-browser"

    def set_available_models(self):
        self.available_models = constants.RENDER_MODELS

    def get_runtime_config(self):
        output = """
* Model customizations:
  * Model: %s
""" % (self.model)
        return output

    async def refresh_session(self, timeout=15):
        """Refresh session, by redirecting the *page* to /api/auth/session rather than a simple xhr request.

        In this way, we can pass the browser check.

        Args:
            timeout (int, optional): Timeout waiting for the refresh in seconds. Defaults to 10.
        """
        self.log.info("Refreshing session...")
        await self.page.goto("https://chat.openai.com/api/auth/session")
        try:
            await self.page.wait_for_url("/api/auth/session", timeout=timeout * 1000)
        except Exception:
            self.log.error("Timed out refreshing session. Page is now at %s. Calling _start_browser()...")
            await self._start_browser()
        try:
            while "Please stand by, while we are checking your browser..." in await self.page.content():
                await asyncio.sleep(1)
            contents = await self.page.content()
            """
            By GETting /api/auth/session, the server would ultimately return a raw json file.
            However, as this is a browser, it will add something to it, like <body> or so, like this:

            <html><head><link rel="stylesheet" href="resource://content-accessible/plaintext.css"></head><body><pre>{xxx:"xxx",{},accessToken="sdjlsfdkjnsldkjfslawefkwnlsdw"}
            </pre></body></html>

            The following code tries to extract the json part from the page, by simply finding the first `{` and the last `}`.
            """
            found_json = re.search('{.*}', contents)
            if found_json is None:
                raise json.JSONDecodeError("Cannot find JSON in /api/auth/session 's response", contents, 0)
            contents = contents[found_json.start():found_json.end()]
            self.log.debug("Refreshing session received: %s", contents)
            self.session = json.loads(contents)
            self.log.info("Succeessfully refreshed session. ")
        except json.JSONDecodeError:
            self.log.error("Failed to decode session key. Maybe Access denied? ")

        # Now the browser should be at /api/auth/session
        # Go back to the chat page.
        await self._start_browser()

    async def _cleanup_divs(self):
        await self.page.evaluate(f"document.getElementById('{self.stream_div_id}').remove()")
        code = (
            """
            const eof_div = document.getElementById('EOF_DIV_ID');
            if(typeof eof_div !== 'undefined' && eof_div !== null) {
              eof_div.remove();
            }
            """
        ).replace("EOF_DIV_ID", self.eof_div_id)
        await self.page.evaluate(code)

    def _api_request_build_headers(self, custom_headers={}):
        headers = {
            "Authorization": f"Bearer {self.session['accessToken']}",
        }
        headers.update(custom_headers)
        return headers

    async def _process_api_response(self, url, response, method="GET"):
        self.log.debug(f"{method} {url} response, OK: {response.ok}, TEXT: {await response.text()}")
        json = None
        if response.ok:
            try:
                json = await response.json()
            except json.JSONDecodeError:
                pass
        if not response.ok or not json:
            self.log.debug(f"{response.status} {response.status_text} {response.headers}")
        return response.ok, json, response

    async def _api_get_request(self, url, query_params={}, custom_headers={}, timeout=None):
        headers = self._api_request_build_headers(custom_headers)
        kwargs = {
            "headers": headers,
            "params": query_params,
        }
        if timeout:
            kwargs["timeout"] = timeout
        self.log.debug(f"GET {url} request, query params: {query_params}, headers: {headers}")
        response = await self.page.request.get(url, **kwargs)
        return await self._process_api_response(url, response)

    async def _api_post_request(self, url, data={}, custom_headers={}, timeout=None):
        headers = self._api_request_build_headers(custom_headers)
        kwargs = {
            "headers": headers,
            "data": data,
        }
        if timeout:
            kwargs["timeout"] = timeout
        self.log.debug(f"POST {url} request, data: {data}, headers: {headers}")
        response = await self.page.request.post(url, **kwargs)
        return await self._process_api_response(url, response, method="POST")

    async def _api_patch_request(self, url, data={}, custom_headers={}, timeout=None):
        headers = self._api_request_build_headers(custom_headers)
        kwargs = {
            "headers": headers,
            "data": data,
        }
        if timeout:
            kwargs["timeout"] = timeout
        self.log.debug(f"PATCH {url} request, data: {data}, headers: {headers}")
        response = await self.page.request.patch(url, **kwargs)
        return await self._process_api_response(url, response, method="PATCH")

    async def _gen_title(self):
        if not self.conversation_id or self.conversation_id and self.conversation_title_set:
            return
        url = f"https://chat.openai.com/backend-api/conversation/gen_title/{self.conversation_id}"
        data = {
            "message_id": self.parent_message_id,
            "model": self.model,
        }
        ok = False
        try:
            ok, json, response = await self._api_post_request(url, data, timeout=GEN_TITLE_TIMEOUT)
        except Exception as e:
            self.log.warning(f"Failed to generate title: {e}")
        if ok:
            # TODO: Do we want to do anything with the title we got back?
            # response_data = response.json()
            self.conversation_title_set = True
        else:
            self.log.warning("Failed to auto-generate title for new conversation")

    def conversation_data_to_messages(self, conversation_data):
        mapping_dict = conversation_data['mapping'].values()
        messages = []
        parent_nodes = [item for item in mapping_dict if 'parent' not in item]
        mapping_dict = [item for item in mapping_dict if 'parent' in item]
        if len(parent_nodes) == 1 and 'children' in parent_nodes[0]:
            parent_id = parent_nodes[0]['children'][0]
        else:
            parent_id = None
        while True:
            current_item = next((item for item in mapping_dict if item['parent'] == parent_id), None)
            if current_item is None:
                return messages
            message = current_item['message']
            if message is not None and 'content' in message and 'author' in message and message['author']['role'] != 'system':
                messages.append({
                    'id': message['id'],
                    'role': message['author']['role'],
                    'message': "".join(message['content']['parts']),
                    'created_time': datetime.datetime.fromtimestamp(int(message['create_time'])),
                })
            parent_id = current_item['id']

    async def delete_conversation(self, uuid=None):
        if self.session is None:
            await self.refresh_session()
        if not uuid and not self.conversation_id:
            return
        id = uuid if uuid else self.conversation_id
        url = f"https://chat.openai.com/backend-api/conversation/{id}"
        data = {
            "is_visible": False,
        }
        ok, json, response = await self._api_patch_request(url, data)
        if ok:
            return ok, json, response
        else:
            return self._handle_error(json, response, "Failed to delete conversation")

    async def set_title(self, title, conversation_id=None):
        if self.session is None:
            await self.refresh_session()
        id = conversation_id if conversation_id else self.conversation_id
        url = f"https://chat.openai.com/backend-api/conversation/{id}"
        data = {
            "title": title,
        }
        ok, json, response = await self._api_patch_request(url, data)
        if ok:
            return ok, json, "Title set"
        else:
            return self._handle_error(json, response, "Failed to set title")

    async def get_history(self, limit=20, offset=0):
        if self.session is None:
            await self.refresh_session()
        url = "https://chat.openai.com/backend-api/conversations"
        query_params = {
            "offset": offset,
            "limit": limit,
        }
        ok, json, response = await self._api_get_request(url, query_params)
        if ok:
            history = {}
            for item in json["items"]:
                item['created_time'] = datetime.datetime.strptime(item['create_time'], "%Y-%m-%dT%H:%M:%S.%f")
                del item['create_time']
                history[item["id"]] = item
            return ok, history, "Retrieved history"
        else:
            return self._handle_error(json, response, "Failed to get history")

    async def get_conversation(self, uuid=None):
        if self.session is None:
            await self.refresh_session()
        uuid = uuid if uuid else self.conversation_id
        if uuid:
            url = f"https://chat.openai.com/backend-api/conversation/{uuid}"
            ok, json, response = await self._api_get_request(url)
            if ok:
                json['id'] = uuid
                return ok, json, response
            else:
                return self._handle_error(json, response, f"Failed to get conversation {uuid}")

    async def ask_stream(self, prompt, title=None, model_customizations={}):
        if self.session is None:
            await self.refresh_session()

        new_message_id = str(uuid.uuid4())

        if "accessToken" not in self.session:
            yield (
                "Your ChatGPT session is not usable.\n"
                "* Run this program with the `install` parameter and log in to ChatGPT.\n"
                "* If you think you are already logged in, try running the `session` command."
            )
            return

        request = {
            "messages": [
                {
                    "id": new_message_id,
                    "role": "user",
                    "content": {"content_type": "text", "parts": [prompt]},
                }
            ],
            "model": self.model,
            "conversation_id": self.conversation_id,
            "parent_message_id": self.parent_message_id,
            "action": "next",
        }

        code = (
            """
            const stream_div = document.createElement('DIV');
            stream_div.id = "STREAM_DIV_ID";
            document.body.appendChild(stream_div);
            const xhr = new XMLHttpRequest();
            xhr.open('POST', 'https://chat.openai.com/backend-api/conversation');
            xhr.setRequestHeader('Accept', 'text/event-stream');
            xhr.setRequestHeader('Content-Type', 'application/json');
            xhr.setRequestHeader('Authorization', 'Bearer BEARER_TOKEN');
            xhr.responseType = 'stream';
            xhr.onreadystatechange = function() {
              var newEvent;
              const interrupt_div = document.getElementById('INTERRUPT_DIV_ID');
              if(xhr.readyState == 3 || xhr.readyState == 4) {
                const newData = xhr.response.substr(xhr.seenBytes);
                try {
                  const newEvents = newData.split(/\\n\\n/).reverse();
                  newEvents.shift();
                  if(newEvents[0] == "data: [DONE]") {
                    newEvents.shift();
                  }
                  if(newEvents.length > 0) {
                    newEvent = newEvents[0].substring(6);
                    // using XHR for eventstream sucks and occasionally ive seen incomplete
                    // json objects come through  JSON.parse will throw if that happens, and
                    // that should just skip until we get a full response.
                    JSON.parse(newEvent);
                  }
                } catch (err) {
                  console.log(err);
                  newEvent = undefined;
                }
                if(newEvent !== undefined) {
                  stream_div.innerHTML = btoa(newEvent);
                  xhr.seenBytes = xhr.responseText.length;
                }
              }
              if(xhr.readyState == 4 && (typeof interrupt_div === 'undefined' || interrupt_div === null)) {
                const eof_div = document.createElement('DIV');
                eof_div.id = "EOF_DIV_ID";
                document.body.appendChild(eof_div);
              }
              if(typeof interrupt_div !== 'undefined' && interrupt_div !== null) {
                console.warn('Interrupting stream');
                xhr.abort();
                interrupt_div.remove();
              }
            };
            xhr.send(JSON.stringify(REQUEST_JSON));
            """.replace(
                "BEARER_TOKEN", self.session["accessToken"]
            )
            .replace("REQUEST_JSON", json.dumps(request))
            .replace("STREAM_DIV_ID", self.stream_div_id)
            .replace("EOF_DIV_ID", self.eof_div_id)
            .replace("INTERRUPT_DIV_ID", self.interrupt_div_id)
        )

        self.log.debug(f"Sending stream request -- model: {self.model}, conversation_id: {self.conversation_id}, parent_message_id: {self.parent_message_id}")
        self.streaming = True
        await self.page.evaluate(code)

        last_event_msg = ""
        start_time = time.time()
        while True:
            if not self.streaming:
                self.log.info("Request to interrupt streaming")
                await self.interrupt_stream()
                break
            eof_datas = await self.page.query_selector_all(f"div#{self.eof_div_id}")

            conversation_datas = await self.page.query_selector_all(
                f"div#{self.stream_div_id}"
            )
            if len(conversation_datas) == 0:
                continue

            full_event_message = None

            try:
                event_raw = base64.b64decode(await conversation_datas[0].inner_html())
                if len(event_raw) > 0:
                    event = json.loads(event_raw)
                    if event is not None:
                        self.parent_message_id = event["message"]["id"]
                        self.conversation_id = event["conversation_id"]
                        full_event_message = "\n".join(
                            event["message"]["content"]["parts"]
                        )
            except Exception:
                yield (
                    "Failed to read response from ChatGPT.  Tips:\n"
                    " * Try again.  ChatGPT can be flaky.\n"
                    " * Use the `session` command to refresh your session, and then try again.\n"
                    " * Restart the program in the `install` mode and make sure you are logged in."
                )
                break

            if full_event_message is not None:
                chunk = full_event_message[len(last_event_msg):]
                self.message_clipboard = last_event_msg = full_event_message
                yield chunk

            # if we saw the eof signal, this was the last event we
            # should process and we are done
            if len(eof_datas) > 0 or (((time.time() - start_time) > self.timeout) and full_event_message is None):
                break

            await asyncio.sleep(0.2)

        if not self.streaming:
            yield (
                "\nGeneration stopped\n"
            )
        self.streaming = False
        await self._cleanup_divs()
        if title:
            await self.set_title(title)
        else:
            await self._gen_title()

    async def interrupt_stream(self):
        self.log.info("Interrupting stream")
        code = (
            """
            const interrupt_div = document.createElement('DIV');
            interrupt_div.id = "INTERRUPT_DIV_ID";
            document.body.appendChild(interrupt_div);
            """
        ).replace("INTERRUPT_DIV_ID", self.interrupt_div_id)
        await self.page.evaluate(code)

    async def ask(self, message, title=None, model_customizations={}):
        """
        Send a message to chatGPT and return the response.

        Args:
            message (str): The message to send.

        Returns:
            str: The response received from OpenAI.
        """
        async with self.lock:
            response = list([i async for i in self.ask_stream(message, title=title)])
            if len(response) == 0:
                return False, response, "Unusable response produced, maybe login session expired. Try 'pkill firefox' and 'chatgpt install'"
            else:
                return True, ''.join(response), "Response received"

    def new_conversation(self):
        super().new_conversation()
        self.parent_message_id = str(uuid.uuid4())


class ChatGPT:

    def __init__(self, config=None, timeout=60, proxy: Optional[ProxySettings] = None):
        self.config = config or Config()
        self.log = Logger(self.__class__.__name__, self.config)
        self.agpt = AsyncChatGPT(config)
        self.async_run(self.agpt.create(timeout, proxy))

    def __getattr__(self, __name: str):
        if hasattr(self.agpt, __name):
            return getattr(self.agpt, __name)
        else:
            raise AttributeError(f"'{self.__class__.__name__}' object has no attribute '{__name}'")

    def async_run(self, awaitable):
        loop = asyncio.get_event_loop()
        if loop.is_running():
            future = asyncio.run_coroutine_threadsafe(awaitable, loop)
            return future.result()
        else:
            return loop.run_until_complete(awaitable)

    def refresh_session(self):
        return self.async_run(self.agpt.refresh_session())

    def ask_stream(self, prompt, title=None, model_customizations={}):
        def iter_over_async(ait):
            loop = asyncio.get_event_loop()
            ait = ait.__aiter__()

            async def get_next():
                try:
                    obj = await ait.__anext__()
                    return False, obj
                except StopAsyncIteration:
                    return True, None
            while True:
                done, obj = loop.run_until_complete(get_next())
                if done:
                    break
                yield obj
        yield from iter_over_async(self.agpt.ask_stream(prompt, title=title))

    def ask(self, message, title=None, model_customizations={}):
        return self.async_run(self.agpt.ask(message, title=title))

    def get_conversation(self, uuid=None):
        return self.async_run(self.agpt.get_conversation(uuid))

    def delete_conversation(self, uuid=None):
        return self.async_run(self.agpt.delete_conversation(uuid))

    def set_title(self, title, conversation_id=None):
        return self.async_run(self.agpt.set_title(title, conversation_id))

    def get_history(self, limit=20, offset=0):
        return self.async_run(self.agpt.get_history(limit, offset))

    def cleanup(self):
        self.async_run(self.agpt.cleanup())
