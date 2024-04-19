import os
import json
from datetime import datetime

from caddy_core.models import CaddyMessageEvent, ApprovalEvent
from caddy_core.services.anonymise import analyse
from caddy_core.services.survey import get_survey, check_if_survey_required
from caddy_core.services import enrolment
from caddy_core.utils.tables import evaluation_table
from caddy_core import components as caddy
from integrations.google_chat import content, responses
from integrations.google_chat.auth import get_google_creds

from fastapi import status
from fastapi.responses import JSONResponse

from googleapiclient.discovery import build

from typing import List
from collections import deque


class GoogleChat:
    def __init__(self):
        self.client = "Google Chat"
        self.messages = content
        self.responses = responses
        self.caddy = build(
            "chat",
            "v1",
            credentials=get_google_creds(os.getenv("CADDY_SERVICE_ACCOUNT")),
        )
        self.supervisor = build(
            "chat",
            "v1",
            credentials=get_google_creds(os.getenv("CADDY_SUPERVISOR_SERVICE_ACCOUNT")),
        )

    def format_message(self, event):
        """
        Receives a message from Google Chat and formats it into a Caddy message event
        """
        space_id = event["space"]["name"].split("/")[1]
        thread_id = None
        if "thread" in event["message"]:
            thread_id = event["message"]["thread"]["name"].split("/")[3]

        message_string = event["message"]["text"].replace("@Caddy", "")

        if "proceed" not in event:
            pii_identified = analyse(message_string)

            if pii_identified:
                # Optionally redact PII from the message by importing redact from services.anonymise
                # message_string = redact(message_string, pii_identified)

                self.send_pii_warning_to_adviser_space(
                    space_id=space_id,
                    thread_id=thread_id,
                    message=self.messages.PII_DETECTED,
                    message_event=event,
                )

                return "PII Detected"

        thread_id, message_id = self.send_dynamic_to_adviser_space(
            response_type="cardsV2",
            space_id=space_id,
            thread_id=thread_id,
            message=self.messages.PROCESSING_MESSAGE,
        )

        caddy_message = CaddyMessageEvent(
            type="PROCESS_CHAT_MESSAGE",
            user=event["user"]["email"],
            name=event["user"]["name"],
            space_id=space_id,
            thread_id=thread_id,
            message_id=message_id,
            message_string=message_string,
            source_client=self.client,
            timestamp=event["eventTime"],
        )

        return caddy_message

    def send_message_to_adviser_space(self, space_id, thread_id, message) -> tuple:
        """
        Sends a message to the adviser space

        Args:
            space_id (str): The ID of the adviser space
            thread_id (str): The ID of the thread
            message (str): The message to be sent

        Returns:
            tuple: A tuple containing the thread_id and message_id of the sent message
        """
        response = (
            self.caddy.spaces()
            .messages()
            .create(
                parent=f"spaces/{space_id}",
                body={
                    "text": message,
                    "thread": {"name": f"spaces/{space_id}/threads/{thread_id}"},
                },
                messageReplyOption="REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD",
            )
            .execute()
        )

        thread_id = response["thread"]["name"].split("/")[3]
        message_id = response["name"].split("/")[3]

        return thread_id, message_id

    def send_pii_warning_to_adviser_space(
        self, space_id: str, thread_id: str, message, message_event
    ):
        self.caddy.spaces().messages().create(
            parent=f"spaces/{space_id}",
            body={
                "cardsV2": [
                    {
                        "cardId": "PIIDetected",
                        "card": {
                            "sections": [
                                {
                                    "widgets": [
                                        {"textParagraph": {"text": message}},
                                    ],
                                },
                                {
                                    "widgets": [
                                        {
                                            "buttonList": {
                                                "buttons": [
                                                    {
                                                        "text": "Proceed without redaction",
                                                        "onClick": {
                                                            "action": {
                                                                "function": "Proceed",
                                                                "parameters": [
                                                                    {
                                                                        "key": "message_event",
                                                                        "value": json.dumps(
                                                                            message_event
                                                                        ),
                                                                    },
                                                                ],
                                                            }
                                                        },
                                                    },
                                                    {
                                                        "text": "Edit original query",
                                                        "onClick": {
                                                            "action": {
                                                                "function": "edit_query_dialog",
                                                                "interaction": "OPEN_DIALOG",
                                                                "parameters": [
                                                                    {
                                                                        "key": "message_event",
                                                                        "value": json.dumps(
                                                                            message_event
                                                                        ),
                                                                    },
                                                                ],
                                                            }
                                                        },
                                                    },
                                                ]
                                            }
                                        }
                                    ],
                                },
                            ],
                        },
                    },
                ],
                "thread": {"name": f"spaces/{space_id}/threads/{thread_id}"},
            },
            messageReplyOption="REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD",
        ).execute()

    def update_message_in_adviser_space(
        self, message_type: str, space_id: str, message_id: str, message
    ) -> None:
        """
        Updates an existing text message in an adviser space

        Args:
            space_id (str): Space of the adviser
            message_id (str): Existing message that requires updating
            message: content to update message with

        Returns:
            None
        """
        match message_type:
            case "text":
                self.caddy.spaces().messages().patch(
                    name=f"spaces/{space_id}/messages/{message_id}",
                    body=message,
                    updateMask="text",
                ).execute()
            case "cardsV2":
                self.caddy.spaces().messages().patch(
                    name=f"spaces/{space_id}/messages/{message_id}",
                    body=message,
                    updateMask="cardsV2",
                ).execute()

    def update_survey_card_in_adviser_space(
        self, space_id: str, message_id: str, card: dict
    ) -> None:
        """
        Updates a survey card in the adviser space given a space ID, message ID, and card

        Args:
            space_id (str): The space ID of the user
            message_id (str): The message ID of the survey card
            card (dict): The card to update

        Returns:
            None
        """
        self.caddy.spaces().messages().patch(
            name=f"spaces/{space_id}/messages/{message_id}",
            body=card,
            updateMask="cardsV2",
        ).execute()

    def get_edit_query_dialog(self, event):
        event = json.loads(event["common"]["parameters"]["message_event"])
        message_string = event["message"]["text"]
        message_string = message_string.replace("@Caddy", "")
        edit_query_dialog = self.edit_query_dialog(event, message_string)

        return JSONResponse(status_code=status.HTTP_200_OK, content=edit_query_dialog)

    def handle_survey_response(self, event):
        question = event["common"]["parameters"]["question"]
        response = event["common"]["parameters"]["response"]
        threadId = event["message"]["thread"]["name"].split("/")[3]
        card = event["message"]["cardsV2"]
        spaceId = event["space"]["name"].split("/")[1]
        messageId = event["message"]["name"].split("/")[3]

        survey_response = [{question: response}]

        evaluation_table.update_item(
            Key={"threadId": str(threadId)},
            UpdateExpression="set surveyResponse = list_append(if_not_exists(surveyResponse, :empty_list), :surveyResponse)",
            ExpressionAttributeValues={
                ":surveyResponse": survey_response,
                ":empty_list": [],
            },
            ReturnValues="UPDATED_NEW",
        )

        remaining_sections = len(card[0]["card"]["sections"])

        for i in range(remaining_sections):
            if (
                card[0]["card"]["sections"][i]["widgets"][1]["textParagraph"]["text"]
                == f"<b>{question}</b>"
            ):
                del card[0]["card"]["sections"][i]
                remaining_sections -= 1
                break

        if remaining_sections == 0:
            card[0]["card"]["sections"].append(self.messages.SURVEY_COMPLETE_WIDGET)

        self.update_survey_card_in_adviser_space(
            space_id=spaceId, message_id=messageId, card={"cardsV2": card}
        )

    def similar_question_dialog(self, similar_question, question_answer, similarity):
        question_dialog = {
            "action_response": {
                "type": "DIALOG",
                "dialog_action": {
                    "dialog": {
                        "body": {
                            "sections": [
                                {
                                    "header": f'<font color="#004f88"><b>{similar_question}</b></font>',
                                    "widgets": [
                                        {"textParagraph": {"text": question_answer}},
                                        {
                                            "textParagraph": {
                                                "text": f'<font color="#004f88"><b>{similarity}% Match</b></font>'
                                            }
                                        },
                                    ],
                                }
                            ]
                        }
                    }
                },
            }
        }
        return question_dialog

    def edit_query_dialog(self, message_event, message_string):
        edit_query_dialog = {
            "action_response": {
                "type": "DIALOG",
                "dialog_action": {
                    "dialog": {
                        "body": {
                            "sections": [
                                {
                                    "header": "PII Detected: Edit query",
                                    "widgets": [
                                        {
                                            "textInput": {
                                                "label": "Please edit your original query to remove PII",
                                                "type": "MULTIPLE_LINE",
                                                "name": "editedQuery",
                                                "value": message_string,
                                            }
                                        },
                                        {
                                            "buttonList": {
                                                "buttons": [
                                                    {
                                                        "text": "Submit edited query",
                                                        "onClick": {
                                                            "action": {
                                                                "function": "receiveEditedQuery",
                                                                "parameters": [
                                                                    {
                                                                        "key": "message_event",
                                                                        "value": json.dumps(
                                                                            message_event
                                                                        ),
                                                                    },
                                                                ],
                                                            }
                                                        },
                                                    }
                                                ]
                                            },
                                            "horizontalAlignment": "END",
                                        },
                                    ],
                                }
                            ]
                        }
                    }
                },
            }
        }
        return edit_query_dialog

    def run_survey(self, survey_card: dict, user_space: str, thread_id: str) -> None:
        """
        Run a survey in the adviser space given a survey card input

        Args:
            survey_card (dict): The survey card to run
            user_space (str): The space ID of the user
            thread_id (str): The thread ID of the conversation

        Returns:
            None
        """
        self.send_dynamic_to_adviser_space(
            response_type="cardsV2",
            space_id=user_space,
            message=survey_card,
            thread_id=thread_id,
        )

    def send_dynamic_to_adviser_space(
        self, response_type: str, space_id: str, message: dict, thread_id: str
    ) -> tuple:
        """
        Sends a dynamic message to the adviser space given a type of response

        Args:
            response_type (str): The type of response to send
            space_id (str): The space ID of the user
            message (dict): The message to send
            thread_id (str): The thread ID of the conversation

        Returns:
            thread_id, message_id
        """
        match response_type:
            case "text":
                response = (
                    self.caddy.spaces()
                    .messages()
                    .create(
                        parent=f"spaces/{space_id}",
                        body={
                            "text": message,
                            "thread": {
                                "name": f"spaces/{space_id}/threads/{thread_id}"
                            },
                        },
                        messageReplyOption="REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD",
                    )
                    .execute()
                )
            case "cardsV2":
                response = (
                    self.caddy.spaces()
                    .messages()
                    .create(
                        parent=f"spaces/{space_id}",
                        body={
                            "cardsV2": message["cardsV2"],
                            "thread": {
                                "name": f"spaces/{space_id}/threads/{thread_id}"
                            },
                        },
                        messageReplyOption="REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD",
                    )
                    .execute()
                )

        thread_id = response["thread"]["name"].split("/")[3]
        message_id = response["name"].split("/")[3]

        return thread_id, message_id

    def run_new_survey(self, user: str, thread_id: str, user_space: str) -> None:
        """
        Run a survey in the adviser space by getting the survey questions and values by providing a user to the get_survey function

        Args:
            survey_card (dict): The survey card to run
            user_space (str): The space ID of the user
            thread_id (str): The thread ID of the conversation

        Returns:
            None
        """
        post_call_survey_questions = get_survey(user)

        survey_card = self.get_post_call_survey_card(post_call_survey_questions)

        self.send_dynamic_to_adviser_space(
            response_type="cardsV2",
            space_id=user_space,
            message=survey_card,
            thread_id=thread_id,
        )

    def get_post_call_survey_card(
        self,
        post_call_survey_questions: List[dict[str, List[str]]],
    ) -> dict:
        """
        Create a post call survey card with the given questions and values

        Args:
            post_call_survey_questions (List[dict[str, List[str]]]): The questions and values for the survey

        Returns:
            dict: The survey card
        """
        card = {
            "cardsV2": [
                {
                    "cardId": "postCallSurvey",
                    "card": {
                        "sections": [],
                    },
                },
            ],
        }

        i = 0
        for question_dict in post_call_survey_questions:
            i += 1
            question = question_dict["question"]
            values = question_dict["values"]
            section = {"widgets": []}

            label_section = {
                "decoratedText": {
                    "topLabel": f"Question {i}",
                }
            }

            question_section = {"textParagraph": {"text": f"<b>{question}</b>"}}

            button_section = {"buttonList": {"buttons": []}}

            for value in values:
                button_section["buttonList"]["buttons"].append(
                    {
                        "text": value,
                        "onClick": {
                            "action": {
                                "function": "survey_response",
                                "parameters": [
                                    {"key": "question", "value": question},
                                    {"key": "response", "value": value},
                                ],
                            }
                        },
                    }
                )

            section["widgets"].append(label_section)
            section["widgets"].append(question_section)
            section["widgets"].append(button_section)

            card["cardsV2"][0]["card"]["sections"].append(section)

        return card

    def create_card(self, llm_response, source_documents):
        card = {
            "cardsV2": [
                {
                    "cardId": "aiResponseCard",
                    "card": {
                        "sections": [],
                    },
                },
            ],
        }

        llm_response_section = {
            "widgets": [
                {"textParagraph": {"text": llm_response.llm_answer}},
            ],
        }

        card["cardsV2"][0]["card"]["sections"].append(llm_response_section)

        reference_links_section = {"header": "Reference links", "widgets": []}

        for document in source_documents:
            reference_link = {
                "textParagraph": {
                    "text": f"<a href=\"{document.metadata['source_url']}\">{document.metadata['source_url']}</a>"
                }
            }
            if reference_link not in reference_links_section["widgets"]:
                reference_links_section["widgets"].append(reference_link)

        card["cardsV2"][0]["card"]["sections"].append(reference_links_section)

        return card

    def send_message_to_supervisor_space(self, space_id, message):
        response = (
            self.supervisor.spaces()
            .messages()
            .create(parent=f"spaces/{space_id}", body=message)
            .execute()
        )

        thread_id = response["thread"]["name"].split("/")[3]
        message_id = response["name"].split("/")[3]

        return thread_id, message_id

    def respond_to_supervisor_thread(self, space_id, message, thread_id):
        self.supervisor.spaces().messages().create(
            parent=f"spaces/{space_id}",
            body={
                "cardsV2": message["cardsV2"],
                "thread": {"name": f"spaces/{space_id}/threads/{thread_id}"},
            },
            messageReplyOption="REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD",
        ).execute()

    # Update message in the supervisor space
    def update_message_in_supervisor_space(
        self, space_id, message_id, new_message
    ):  # find message name
        self.supervisor.spaces().messages().patch(
            name=f"spaces/{space_id}/messages/{message_id}",
            updateMask="cardsV2",
            body=new_message,
        ).execute()

    # Update message in the adviser space
    def update_dynamic_message_in_adviser_space(
        self, space_id, message_id, response_type, message
    ):
        self.caddy.spaces().messages().patch(
            name=f"spaces/{space_id}/messages/{message_id}",
            updateMask=response_type,
            body=message,
        ).execute()

    # Delete message in the adviser space
    def delete_message_in_adviser_space(self, space_id, message_id):
        self.caddy.spaces().messages().delete(
            name=f"spaces/{space_id}/messages/{message_id}"
        ).execute()

    def create_supervision_request_card(self, user, initial_query):
        request_awaiting = self.responses.supervisor_request_pending(
            user, initial_query
        )

        request_approved = self.responses.supervisor_request_approved(
            user, initial_query
        )

        request_rejected = self.responses.supervisor_request_rejected(
            user, initial_query
        )

        return request_awaiting, request_approved, request_rejected

    def create_supervision_card(
        self,
        user_email,
        event,
        new_request_message_id,
        request_approved,
        request_rejected,
    ):
        card_for_approval = event.llm_response_json
        conversation_id = event.conversation_id
        response_id = event.response_id
        message_id = event.message_id
        thread_id = event.thread_id

        approval_buttons_section = {
            "widgets": [
                {
                    "buttonList": {
                        "buttons": [
                            {
                                "text": "👍",
                                "onClick": {
                                    "action": {
                                        "function": "Approved",
                                        "parameters": [
                                            {
                                                "key": "aiResponse",
                                                "value": json.dumps(card_for_approval),
                                            },
                                            {
                                                "key": "conversationId",
                                                "value": conversation_id,
                                            },
                                            {"key": "responseId", "value": response_id},
                                            {"key": "messageId", "value": message_id},
                                            {"key": "threadId", "value": thread_id},
                                            {
                                                "key": "newRequestId",
                                                "value": new_request_message_id,
                                            },
                                            {
                                                "key": "requestApproved",
                                                "value": json.dumps(request_approved),
                                            },
                                            {"key": "userEmail", "value": user_email},
                                        ],
                                    }
                                },
                            },
                            {
                                "text": "👎",
                                "onClick": {
                                    "action": {
                                        "function": "rejected_dialog",
                                        "interaction": "OPEN_DIALOG",
                                        "parameters": [
                                            {
                                                "key": "conversationId",
                                                "value": conversation_id,
                                            },
                                            {"key": "responseId", "value": response_id},
                                            {"key": "messageId", "value": message_id},
                                            {"key": "threadId", "value": thread_id},
                                            {
                                                "key": "newRequestId",
                                                "value": new_request_message_id,
                                            },
                                            {
                                                "key": "requestRejected",
                                                "value": json.dumps(request_rejected),
                                            },
                                            {"key": "userEmail", "value": user_email},
                                        ],
                                    }
                                },
                            },
                        ]
                    }
                }
            ],
        }

        card_for_approval_sections = deque(
            card_for_approval["cardsV2"][0]["card"]["sections"]
        )

        card_for_approval_sections.append(approval_buttons_section)

        card_for_approval_sections = list(card_for_approval_sections)

        card_for_approval["cardsV2"][0]["card"]["sections"] = card_for_approval_sections

        return card_for_approval

    def handle_new_supervision_event(self, user, supervisor_space, event):
        (
            request_awaiting,
            request_approved,
            request_rejected,
        ) = self.create_supervision_request_card(
            user=user, initial_query=event.llmPrompt
        )
        (
            new_request_thread,
            new_request_message_id,
        ) = self.send_message_to_supervisor_space(
            space_id=supervisor_space, message=request_awaiting
        )

        card = self.create_supervision_card(
            user_email=user,
            event=event,
            new_request_message_id=new_request_message_id,
            request_approved=request_approved,
            request_rejected=request_rejected,
        )

        self.respond_to_supervisor_thread(
            space_id=supervisor_space, message=card, thread_id=new_request_thread
        )

    def create_approved_card(self, card, approver):
        card["cardsV2"][0]["card"]["sections"].append(
            self.responses.approval_json_widget(approver)
        )

        return card

    def received_approval(self, event):
        card = json.loads(event["common"]["parameters"]["aiResponse"])
        user_space = event["common"]["parameters"]["conversationId"]
        approver = event["user"]["email"]
        response_id = event["common"]["parameters"]["responseId"]
        thread_id = event["common"]["parameters"]["threadId"]
        supervisor_space = event["space"]["name"].split("/")[1]
        message_id = event["message"]["name"].split("/")[3]
        supervisor_card = {"cardsV2": event["message"]["cardsV2"]}
        user_message_id = event["common"]["parameters"]["messageId"]
        request_message_id = event["common"]["parameters"]["newRequestId"]
        request_card = json.loads(event["common"]["parameters"]["requestApproved"])
        user_email = event["common"]["parameters"]["userEmail"]

        approved_card = self.create_approved_card(card=card, approver=approver)

        updated_supervision_card = self.create_updated_supervision_card(
            supervision_card=supervisor_card,
            approver=approver,
            approved=True,
            supervisor_message="",
        )
        self.update_message_in_supervisor_space(
            space_id=supervisor_space,
            message_id=message_id,
            new_message=updated_supervision_card,
        )

        self.update_message_in_supervisor_space(
            space_id=supervisor_space,
            message_id=request_message_id,
            new_message=request_card,
        )

        self.update_dynamic_message_in_adviser_space(
            space_id=user_space,
            message_id=user_message_id,
            response_type="cardsV2",
            message=approved_card,
        )

        approval_event = ApprovalEvent(
            response_id=response_id,
            thread_id=thread_id,
            approver_email=approver,
            approved=True,
            approval_timestamp=event["eventTime"],
            user_response_timestamp=datetime.now(),
            supervisor_message=None,
        )

        return user_email, user_space, thread_id, approval_event

    def handle_supervisor_rejection(self, event):
        supervisor_card = {"cardsV2": event["message"]["cardsV2"]}
        user_space = event["common"]["parameters"]["conversationId"]
        approver = event["user"]["email"]
        response_id = event["common"]["parameters"]["responseId"]
        supervisor_space = event["space"]["name"].split("/")[1]
        message_id = event["message"]["name"].split("/")[3]
        user_message_id = event["common"]["parameters"]["messageId"]
        supervisor_message = event["common"]["formInputs"]["supervisorResponse"][
            "stringInputs"
        ]["value"][0]
        thread_id = event["common"]["parameters"]["threadId"]
        request_message_id = event["common"]["parameters"]["newRequestId"]
        request_card = json.loads(event["common"]["parameters"]["requestRejected"])
        user_email = event["common"]["parameters"]["userEmail"]

        self.update_message_in_supervisor_space(
            space_id=supervisor_space,
            message_id=request_message_id,
            new_message=request_card,
        )

        self.update_dynamic_message_in_adviser_space(
            space_id=user_space,
            message_id=user_message_id,
            response_type="cardsV2",
            message=self.responses.supervisor_rejection(
                approver=approver, supervisor_message=supervisor_message
            ),
        )

        updated_supervision_card = self.create_updated_supervision_card(
            supervision_card=supervisor_card,
            approver=approver,
            approved=False,
            supervisor_message=supervisor_message,
        )
        self.update_message_in_supervisor_space(
            space_id=supervisor_space,
            message_id=message_id,
            new_message=updated_supervision_card,
        )

        rejection_event = ApprovalEvent(
            response_id=response_id,
            thread_id=thread_id,
            approver_email=approver,
            approved=False,
            approval_timestamp=event["eventTime"],
            user_response_timestamp=datetime.now(),
            supervisor_message=supervisor_message,
        )

        caddy.store_approver_event(rejection_event)

        self.call_complete_confirmation(user_email, user_space, thread_id)

    def create_updated_supervision_card(
        self, supervision_card, approver, approved, supervisor_message
    ):
        if approved:
            approval_section = self.responses.approval_json_widget(approver)
        else:
            approval_section = self.responses.rejection_json_widget(
                approver, supervisor_message
            )

        card_for_approval_sections = deque(
            supervision_card["cardsV2"][0]["card"]["sections"]
        )
        card_for_approval_sections.pop()  # remove thumbs up/ thumbs down section
        card_for_approval_sections.append(approval_section)

        card_for_approval_sections = list(card_for_approval_sections)

        supervision_card["cardsV2"][0]["card"]["sections"] = card_for_approval_sections

        return supervision_card

    def create_rejected_card(self, card, approver):
        rejection_json = self.responses.rejection_json_widget(approver)

        card["cardsV2"][0]["card"]["sections"].append(rejection_json)

        return card

    def user_list_dialog(self, supervision_users: str, space_display_name: str):
        list_dialog = {
            "action_response": {
                "type": "DIALOG",
                "dialog_action": {
                    "dialog": {
                        "body": {
                            "sections": [
                                {
                                    "header": f"Supervision users for {space_display_name}",
                                    "widgets": [
                                        {"textParagraph": {"text": supervision_users}}
                                    ],
                                }
                            ]
                        }
                    }
                },
            }
        }
        return list_dialog

    def failed_dialog(self, error):
        print(f"### FAILED: {error} ###")

    def get_supervisor_response_dialog(
        self,
        conversation_id,
        response_id,
        message_id,
        thread_id,
        new_request_message_id,
        request_rejected,
        user_email,
    ):
        supervisor_response_dialog = {
            "action_response": {
                "type": "DIALOG",
                "dialog_action": {
                    "dialog": {
                        "body": {
                            "sections": [
                                {
                                    "header": "Rejected response follow up",
                                    "widgets": [
                                        {
                                            "textInput": {
                                                "label": "Enter a valid response for the adviser to their question",
                                                "type": "MULTIPLE_LINE",
                                                "name": "supervisorResponse",
                                            }
                                        },
                                        {
                                            "buttonList": {
                                                "buttons": [
                                                    {
                                                        "text": "Submit response",
                                                        "onClick": {
                                                            "action": {
                                                                "function": "receiveSupervisorResponse",
                                                                "parameters": [
                                                                    {
                                                                        "key": "conversationId",
                                                                        "value": conversation_id,
                                                                    },
                                                                    {
                                                                        "key": "responseId",
                                                                        "value": response_id,
                                                                    },
                                                                    {
                                                                        "key": "messageId",
                                                                        "value": message_id,
                                                                    },
                                                                    {
                                                                        "key": "threadId",
                                                                        "value": thread_id,
                                                                    },
                                                                    {
                                                                        "key": "newRequestId",
                                                                        "value": new_request_message_id,
                                                                    },
                                                                    {
                                                                        "key": "requestRejected",
                                                                        "value": request_rejected,
                                                                    },
                                                                    {
                                                                        "key": "userEmail",
                                                                        "value": user_email,
                                                                    },
                                                                ],
                                                            }
                                                        },
                                                    }
                                                ]
                                            },
                                            "horizontalAlignment": "END",
                                        },
                                    ],
                                }
                            ]
                        }
                    }
                },
            }
        }
        return supervisor_response_dialog

    def get_supervisor_response(self, event):
        """
        Upon supervisor rejection returns a dialog box for supervisor response

        Args:
            Google Chat Event

        Returns:
            Google Chat Dialog
        """
        conversation_id = event["common"]["parameters"]["conversationId"]
        response_id = event["common"]["parameters"]["responseId"]
        message_id = event["common"]["parameters"]["messageId"]
        thread_id = event["common"]["parameters"]["threadId"]
        new_request_message_id = event["common"]["parameters"]["newRequestId"]
        request_rejected = event["common"]["parameters"]["requestRejected"]
        user_email = event["common"]["parameters"]["userEmail"]

        dialog = self.get_supervisor_response_dialog(
            conversation_id,
            response_id,
            message_id,
            thread_id,
            new_request_message_id,
            request_rejected,
            user_email,
        )

        return dialog

    def add_user(self, event):
        user = event["common"]["formInputs"]["email"]["stringInputs"]["value"][0]
        role = event["common"]["formInputs"]["role"]["stringInputs"]["value"][0]
        supervisor_space_id = event["space"]["name"].split("/")[1]

        try:
            enrolment.register_user(user, role, supervisor_space_id)
        except Exception as error:
            print(f"Adding user failed: {error}")

    def remove_user(self, event):
        user = event["common"]["formInputs"]["email"]["stringInputs"]["value"][0]

        try:
            enrolment.remove_user(user)
        except Exception as error:
            print(f"Adding user failed: {error}")

    def list_space_users(self, event):
        supervision_space_id = event["space"]["name"].split("/")[1]
        space_name = event["space"]["displayName"]

        space_users = enrolment.list_users(supervision_space_id)

        return self.user_list_dialog(
            supervision_users=space_users, space_display_name=space_name
        )

    def get_survey(self, user: str) -> dict:
        """
        Gets a post call survey card for the given user

        Args:
            user (str): The email of the user

        Returns:
            dict: The survey card
        """
        post_call_survey_questions = get_survey(user)

        survey_card = self.get_post_call_survey_card(post_call_survey_questions)

        return survey_card

    def call_complete_confirmation(
        self, user: str, user_space: str, thread_id: str
    ) -> None:
        """
        Send a card to the adviser space to confirm the call is complete

        Args:
            user (str): The email of the user
            user_space (str): The space ID of the user
            thread_id (str): The thread ID of the conversation

        Returns:
            None
        """
        survey_card = self.get_survey(user)
        call_complete_card = {
            "cardsV2": [
                {
                    "cardId": "callCompleteCard",
                    "card": {
                        "sections": [
                            {
                                "widgets": [
                                    {
                                        "buttonList": {
                                            "buttons": [
                                                {
                                                    "text": "Mark call complete",
                                                    "onClick": {
                                                        "action": {
                                                            "function": "call_complete",
                                                            "parameters": [
                                                                {
                                                                    "key": "survey",
                                                                    "value": json.dumps(
                                                                        survey_card
                                                                    ),
                                                                },
                                                            ],
                                                        }
                                                    },
                                                }
                                            ]
                                        }
                                    }
                                ]
                            }
                        ],
                    },
                },
            ],
        }

        self.send_dynamic_to_adviser_space(
            response_type="cardsV2",
            space_id=user_space,
            message=call_complete_card,
            thread_id=thread_id,
        )

    def finalise_caddy_call(self, event) -> None:
        """
        Marks a call as complete and triggers post call survey upon user triggered event

        Args:
            Google Chat Event

        Returns:
            None
        """
        survey_card = json.loads(event["common"]["parameters"]["survey"])
        thread_id = event["message"]["thread"]["name"].split("/")[3]
        user_space = event["space"]["name"].split("/")[1]
        user = event["user"]["email"]
        caddy.mark_call_complete(user=user, thread_id=thread_id)
        survey_required = check_if_survey_required(user)
        if survey_required is True:
            self.update_survey_card_in_adviser_space(
                space_id=user_space,
                message_id=event["message"]["name"].split("/")[3],
                card=self.messages.CALL_COMPLETE,
            )
            self.run_survey(survey_card, user_space, thread_id)

    def handle_edited_query(self, event) -> CaddyMessageEvent:
        """
        Handles a edited query event from PII detected message

        Args:
            Google Chat Event

        Returns:
            CaddyMessageEvent
        """
        edited_message = event["common"]["formInputs"]["editedQuery"]["stringInputs"][
            "value"
        ][0]
        event = json.loads(event["common"]["parameters"]["message_event"])
        event["message"]["text"] = edited_message
        event["proceed"] = True
        caddy_message = self.format_message(event)
        return caddy_message

    def handle_proceed_query(self, event) -> CaddyMessageEvent:
        """
        Handles a proceed overwrite event from PII detected message

        Args:
            Google Chat Event

        Returns:
            CaddyMessageEvent
        """
        event = json.loads(event["common"]["parameters"]["message_event"])
        event["proceed"] = True
        return self.format_message(event)

    def handle_supervisor_approval(self, event):
        (
            user,
            user_space,
            thread_id,
            approval_event,
        ) = self.received_approval(event)
        caddy.store_approver_event(approval_event)
        self.call_complete_confirmation(user, user_space, thread_id)